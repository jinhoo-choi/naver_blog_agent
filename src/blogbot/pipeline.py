from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

from .config import Settings
from .core import (
    connect_db, load_post, mark_saved, max_title_similarity, recent_titles,
    render_post_text, reserve_attempt, review_result, save_post, set_status,
    today_kst, validate_post,
)
from .llm import BlogLLM
from .naver import NaverDraftWriter


def make_writer(settings: Settings) -> NaverDraftWriter:
    return NaverDraftWriter(
        blog_id=settings.naver_blog_id, profile_dir=settings.naver_profile_dir,
        naver_id=settings.naver_id, naver_password=settings.naver_password,
        headless=settings.headless, selectors=settings.config.get("naver", {}),
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
            except Exception as exc:
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
    llm = BlogLLM(
        settings.openai_api_key, settings.openai_model, settings.root, settings.review_model,
    )
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    with closing(connect_db(settings.db_path)) as conn:
        for _ in range(requested):
            reservation = reserve_attempt(conn, settings.config, requested)
            if reservation is None:
                break
            attempt_id, category_key = reservation
            result = {"attempt": attempt_id, "category": category_key}
            try:
                info = settings.config["categories"][category_key]
                existing = recent_titles(conn)
                post = llm.create_draft(category_key, info, existing)
                validate_post(post, info)
                threshold = float(limits["max_similarity"])
                if max_title_similarity(post.title, existing) >= threshold:
                    result["status"] = "DROP_DUPLICATE"
                else:
                    review = llm.review(post, info)
                    score, decision = review_result(review)
                    if decision == "REWRITE" and score >= int(limits["rewrite_score"]):
                        post = llm.rewrite(post, info, review)
                        validate_post(post, info)
                        review = llm.review(post, info)
                        score, decision = review_result(review)
                    if max_title_similarity(post.title, recent_titles(conn)) >= threshold:
                        result["status"] = "DROP_DUPLICATE"
                    elif decision != "PASS" or score < int(limits["review_pass_score"]):
                        result.update(status="DROP_REVIEW", score=score)
                    else:
                        post.quality_score, post.status = score, "APPROVED"
                        post_id = save_post(conn, post)
                        result.update(id=post_id, status="APPROVED", score=score)
                        stem = settings.artifact_dir / f"{post.as_of_date}-{post_id:05d}"
                        stem.with_suffix(".json").write_text(
                            json.dumps({"post": asdict(post), "review": review},
                                       ensure_ascii=False, indent=2), encoding="utf-8",
                        )
                        stem.with_suffix(".md").write_text(
                            f"# {post.title}\n\n{render_post_text(post)}\n", encoding="utf-8",
                        )
            except sqlite3.IntegrityError:
                conn.rollback()
                result["status"] = "DROP_DUPLICATE"
            except Exception as exc:
                result.update(status="ERROR", error=type(exc).__name__)
            with conn:
                conn.execute("UPDATE attempts SET status=? WHERE id=?", (result["status"], attempt_id))
            results.append(result)
    if save_to_naver:
        results.extend(save_pending(settings))
    return results or [{"status": "DAILY_LIMIT_REACHED"}]
