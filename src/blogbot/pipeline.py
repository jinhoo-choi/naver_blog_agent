from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import timedelta

from openai import OpenAIError
from playwright.sync_api import Error as PlaywrightError

from .config import Settings
from .core import (
    connect_db,
    load_post,
    mark_saved,
    max_title_similarity,
    recent_titles,
    render_post_text,
    reserve_attempt,
    review_result,
    save_post,
    set_status,
    today_kst,
    validate_post,
)
from .images import ImagePending, atomic_json, generate_images
from .inputs import ContentRequest, collect_requests
from .llm import BlogLLM
from .naver import NaverDraftWriter
from .presentation import validate_structure
from .research import ResearchRequired, prepare_request
from .topics import rank_candidates


def make_writer(settings: Settings) -> NaverDraftWriter:
    return NaverDraftWriter(
        blog_id=settings.naver_blog_id, profile_dir=settings.naver_profile_dir,
        naver_id=settings.naver_id, naver_password=settings.naver_password,
        headless=settings.headless, selectors=settings.config.get("naver", {}),
        categories=settings.config["categories"],
    )


def save_pending(settings: Settings) -> list[dict]:
    """Retry reviewed text without buying another generation or repeating uncertain saves."""
    writer = make_writer(settings)
    results = []
    with closing(connect_db(settings.db_path)) as conn:
        uncertain = conn.execute(
            "SELECT id FROM posts WHERE status IN ('SAVING', 'SAVE_UNCERTAIN') LIMIT 1"
        ).fetchone()
        if uncertain:
            return [{"id": uncertain[0], "status": "MANUAL_CHECK_REQUIRED"}]
        rows = conn.execute("SELECT * FROM posts WHERE status='APPROVED' ORDER BY id").fetchall()
        for row in rows:
            post = load_post(row)
            if post.as_of_date != today_kst().isoformat():
                set_status(conn, row["id"], "STALE_REVIEW_REQUIRED")
                results.append({"id": row["id"], "status": "STALE_REVIEW_REQUIRED"})
                continue
            try:
                writer.preflight(post)
            except (OSError, ValueError, RuntimeError) as exc:
                results.append({"id": row["id"], "status": "SETUP_REQUIRED",
                                "error": type(exc).__name__})
                continue
            # Claim before opening a browser. Crashes leave SAVING for manual resolution.
            with conn:
                claimed = conn.execute(
                    "UPDATE posts SET status='SAVING' WHERE id=? AND status='APPROVED'",
                    (row["id"],),
                ).rowcount
            if not claimed:
                continue
            result = {"id": row["id"], "category": post.category, "score": post.quality_score}
            try:
                writer.save(post)
                mark_saved(conn, row["id"])
                result["status"] = "SAVED_NAVER"
            except (PlaywrightError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                # The editor may autosave even before a click: never retry blindly.
                set_status(conn, row["id"], "SAVE_UNCERTAIN")
                result.update(status="SAVE_UNCERTAIN", error=type(exc).__name__)
                results.append(result)
                break
            results.append(result)
    return results


def run_daily(settings: Settings, count: int | None = None, save_to_naver: bool = False) -> list[dict]:
    requested = settings.daily_count if count is None else count
    limits = settings.config["blog"]
    if not int(limits["daily_min"]) <= requested <= int(limits["daily_max"]):
        raise ValueError("Daily count must be between 1 and 5")
    if save_to_naver:
        make_writer(settings)
        with closing(connect_db(settings.db_path)) as conn:
            uncertain = conn.execute(
                "SELECT id FROM posts WHERE status IN ('SAVING', 'SAVE_UNCERTAIN') LIMIT 1"
            ).fetchone()
            if uncertain:
                return [{"id": uncertain[0], "status": "MANUAL_CHECK_REQUIRED"}]
    candidates, notices = collect_requests(settings)
    llm = None
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = list(notices)
    with closing(connect_db(settings.db_path)) as conn:
        candidates = rank_candidates(settings, conn, candidates, requested)
        # Resume media only. Never purchase a new writer/reviewer call for approved text.
        pending = conn.execute("SELECT * FROM posts WHERE status IN "
                               "('TEXT_APPROVED', 'IMAGES_PENDING') ORDER BY id").fetchall()
        for row in pending:
            post = load_post(row)
            today = today_kst()
            if not (today-timedelta(days=3)).isoformat() <= post.as_of_date <= today.isoformat():
                continue
            stem = settings.artifact_dir / f"{post.as_of_date}-{row['id']:05d}"
            payload = json.loads(stem.with_suffix('.json').read_text(encoding='utf-8'))
            request = ContentRequest(**payload['input'])
            results.append(complete_media(settings, conn, row['id'], post, request, payload['review']))
        for _ in range(requested):
            reservation = reserve_attempt(conn, settings.config, requested, candidates)
            if reservation is None:
                break
            attempt_id, request = reservation
            category_key = request.category
            result = {"attempt": attempt_id, "category": category_key}
            try:
                info = settings.config["categories"][category_key]
                request = prepare_request(settings, request)
                if llm is None:
                    llm = BlogLLM(settings.openai_api_key, settings.openai_model,
                                  settings.root, settings.review_model)
                existing = recent_titles(conn)
                context_path = settings.db_path.parent / 'context.json'
                if context_path.exists():
                    existing += json.loads(context_path.read_text()).get('published_titles', [])
                post = llm.create_draft(request, info, existing)
                validate_post(post, info)
                validate_structure(post, settings.config.get("editorial", {}).get("require_structure", True))
                threshold = float(limits["max_similarity"])
                if max_title_similarity(post.title, existing) >= threshold:
                    result["status"] = "DROP_DUPLICATE"
                else:
                    review = llm.review(post, info, request)
                    score, decision = review_result(review)
                    if decision == "REWRITE" and score >= int(limits["rewrite_score"]):
                        post = llm.rewrite(post, info, review, request)
                        validate_post(post, info)
                        validate_structure(post, settings.config.get("editorial", {}).get("require_structure", True))
                        review = llm.review(post, info, request)
                        score, decision = review_result(review)
                    if max_title_similarity(post.title, recent_titles(conn)) >= threshold:
                        result["status"] = "DROP_DUPLICATE"
                    elif decision != "PASS" or score < int(limits["review_pass_score"]):
                        result.update(status="DROP_REVIEW", score=score)
                    else:
                        post.quality_score, post.status = score, "TEXT_APPROVED"
                        post_id = save_post(conn, post)
                        stem = settings.artifact_dir / f"{post.as_of_date}-{post_id:05d}"
                        atomic_json(stem.with_suffix('.json'),
                                    {'post': asdict(post), 'review': review, 'input': asdict(request)})
                        result.update(complete_media(settings, conn, post_id, post, request, review))
            except ResearchRequired:
                result["status"] = "RESEARCH_REQUIRED"
            except sqlite3.IntegrityError:
                conn.rollback()
                result["status"] = "DROP_DUPLICATE"
            except (OpenAIError, PlaywrightError, OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
                result.update(status="ERROR", error=type(exc).__name__)
            with conn:
                conn.execute("UPDATE attempts SET status=? WHERE id=?", (result["status"], attempt_id))
            results.append(result)
    if save_to_naver:
        results.extend(save_pending(settings))
    return results or [{"status": "NO_ELIGIBLE_INPUT_OR_DAILY_LIMIT"}]


def complete_media(settings, conn, post_id, post, request, review):
    stem = settings.artifact_dir / f"{post.as_of_date}-{post_id:05d}"
    try:
        post = generate_images(settings, request, post)
    except ImagePending:
        set_status(conn, post_id, 'IMAGES_PENDING')
        return {'id': post_id, 'category': post.category, 'status': 'IMAGES_PENDING'}
    post.status = 'APPROVED'
    # Write the durable packet before marking it ready in the database.
    atomic_json(stem.with_suffix('.json'),
                {'post': asdict(post), 'review': review, 'input': asdict(request)})
    stem.with_suffix('.md').write_text(
        f'# {post.title}\n\n{render_post_text(post)}\n', encoding='utf-8')
    with conn:
        conn.execute("UPDATE posts SET photos_json=?, status='APPROVED' WHERE id=?",
                     (json.dumps(post.photos, ensure_ascii=False), post_id))
    return {'id': post_id, 'category': post.category, 'status': 'APPROVED', 'score': post.quality_score}
