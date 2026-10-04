"""Explicit, bounded editorial recovery of existing rejected responses."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, replace

from cryptography.fernet import Fernet, InvalidToken
from openai import OpenAIError

from .core import (
    PostDraft,
    max_title_similarity,
    review_result,
    save_post,
    today_kst,
    validate_post,
)
from .images import atomic_json
from .inputs import ContentRequest
from .llm import BlogLLM, _checked_review, _historical_source_urls, _source_urls
from .planning import active_plan, attempt_matches, reservation_result
from .presentation import normalize_structure, validate_structure
from .research import prepare_reference_evidence, prepare_request


def rejected_checkpoint(settings, request, *, raw=False):
    directory = settings.db_path.parent
    ids, positions = {}, {}
    for position, line in enumerate((directory / 'usage.jsonl').read_text().splitlines()):
        entry = json.loads(line)
        if entry['request_id'] == request.id and not entry.get('error') and entry.get('response_id'):
            ids[entry['stage']] = entry['response_id']
            positions[entry['stage']] = position
    cached = {}
    for path in (directory / 'response-cache').glob(f'{today_kst()}-*.json'):
        item = json.loads(path.read_text())
        for stage, response_id in ids.items():
            if item.get('response', {}).get('id') == response_id:
                cached[stage] = item
    original = cached['writer']
    latest = cached.get('rewrite', original)
    data = latest['payload']
    if raw:
        from .pre_review import DraftCandidate
        return DraftCandidate(data, _historical_source_urls(directory / 'usage.jsonl', request.id))
    reviewer = cached.get('reviewer', {})
    sources = _source_urls(data, _historical_source_urls(directory / 'usage.jsonl', request.id))
    request = prepare_reference_evidence(directory, request, sources)
    post = PostDraft(request.category, data['subcategory'], original['payload']['title'],
                     data['title'], data['body'], data['tags'], sources, str(today_kst()),
                     request_id=request.id, photos=request.photos, provenance=request.provenance)
    reviewed_latest = positions.get('reviewer', -1) > max(
        positions.get('writer', -1), positions.get('rewrite', -1))
    return {'post': asdict(post), 'input': asdict(request),
            'review': _checked_review(reviewer.get('payload', {}), reviewer.get('response', {}), request)
            if reviewed_latest else {}}


def editorial_patch(settings, post):
    """Apply an explicit dated operator correction; never confer approval."""
    key = hashlib.sha256(post.request_id.encode()).hexdigest()
    path = settings.root / 'editorial' / post.as_of_date / f'{key}.enc'
    if not path.exists():
        return post
    try:
        data = json.loads(Fernet(os.environ['BLOG_BUNDLE_KEY']).decrypt(path.read_bytes()))
    except InvalidToken as exc:
        raise ValueError('Editorial correction decryption failed') from exc
    if (set(data) - {'request_id', 'as_of_date', 'title', 'body', 'source_urls'}
            or data.get('request_id') != post.request_id
            or data.get('as_of_date') != post.as_of_date):
        raise ValueError('Editorial correction identity mismatch')
    for name in ('title', 'body'):
        if name in data and (not isinstance(data[name], str) or not data[name].strip()):
            raise ValueError('Editorial correction must contain nonempty text')
    sources = data.get('source_urls', post.source_urls)
    if (not isinstance(sources, list) or not sources
            or any(url not in post.source_urls for url in sources)):
        raise ValueError('Editorial correction cannot add unobserved sources')
    return replace(post, title=data.get('title', post.title), body=data.get('body', post.body),
                   source_urls=sources)


def recover_rejected(settings, conn, candidates):
    from .pipeline import complete_media
    results, handled = [], set()
    plan = active_plan(settings)
    if plan and plan.get('reservation'):
        return [reservation_result(plan)]
    by_id = {request.id: request for request in candidates}
    ready = {row[0] for row in conn.execute(
        "SELECT category FROM posts WHERE as_of_date=? AND status IN "
        "('APPROVED','TEXT_APPROVED','IMAGES_PENDING','SAVED_NAVER')", (str(today_kst()),))}
    for row in conn.execute("SELECT * FROM attempts WHERE day=? AND status='DROP_REVIEW' ORDER BY id",
                            (str(today_kst()),)).fetchall():
        category = row['category']
        if plan and not attempt_matches(row, plan):
            continue
        if settings.config['categories'][category]['max_daily'] == 0:
            continue
        if category in ready or category in handled:
            continue
        handled.add(category)  # At most one existing manuscript per missing category.
        result = {'attempt': row['id'], 'category': category, 'request_id': row['request_id']}
        from .pre_review import checkpoint_path, revision_path
        if checkpoint_path(settings.db_path.parent, row['request_id']).exists():
            # New automatic drafts share a durable single-correction budget.
            # Explicit legacy recovery must not silently grant them extra rewrites.
            results.append({**result, 'status': 'DROP_REVIEW',
                            'reason': ('manuscript_correction_limit' if revision_path(
                                settings.db_path.parent, row['request_id']).exists()
                                       else 'review_rejected_after_pre_review')})
            continue
        key = hashlib.sha256(row['request_id'].encode()).hexdigest()
        path = settings.db_path.parent / 'response-cache' / f'{today_kst()}-recovery-{key}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists():
                payload = json.loads(path.read_text())
                latest = rejected_checkpoint(settings, ContentRequest(**payload['input']))
                if (latest['review'] and latest['post']['body'] != payload['post']['body']
                        and payload.get('revision_attempted')):
                    payload = {**latest, 'revision_count': payload.get('revision_count', 0) + 1}
            else:
                request = prepare_request(settings, by_id[row['request_id']])
                payload = rejected_checkpoint(settings, request)
            request = ContentRequest(**payload['input'])
            post = PostDraft(**payload['post'])
            info = settings.config['categories'][category]
            validate_post(post, info)
            review = payload['review']
            corrected = editorial_patch(settings, post)
            if corrected != post:
                validate_post(corrected, info)
                validate_structure(corrected, settings.config.get('editorial', {}).get('require_structure', True))
                llm = BlogLLM(settings.openai_api_key, settings.openai_model, settings.root,
                              settings.review_model, settings.db_path.parent / 'usage.jsonl')
                review = llm.review(corrected, info, request)
                post = corrected
                payload = {'post': asdict(post), 'input': asdict(request), 'review': review,
                           'revision_count': payload.get('revision_count', 0)}
                atomic_json(path, payload)
            if review_result(review)[1] != 'PASS':
                resumed = bool(payload.get('revision_attempted'))
                if not resumed and payload.get('revision_count', 0) >= 2:
                    results.append({**result, 'status': 'DROP_REVIEW', 'reason': 'editorial_revision_limit'})
                    continue
                atomic_json(path, {**payload, 'revision_attempted': True})
                llm = BlogLLM(settings.openai_api_key, settings.openai_model, settings.root,
                              settings.review_model, settings.db_path.parent / 'usage.jsonl')
                post = llm.rewrite(post, info, review, request, cache_only=resumed)
                post = normalize_structure(post)
                validate_post(post, info)
                review = llm.review(post, info, request)
                atomic_json(path, {'post': asdict(post), 'input': asdict(request), 'review': review,
                                   'revision_count': payload.get('revision_count', 0) + 1})
            post = normalize_structure(post)
            validate_structure(post, settings.config.get('editorial', {}).get('require_structure', True))
            score, decision = review_result(review)
            other_titles = [r[0] for r in conn.execute(
                'SELECT title FROM posts WHERE request_id!=?', (post.request_id,))]
            if max_title_similarity(post.title, other_titles) >= float(settings.config['blog']['max_similarity']):
                decision = 'DROP'
            if decision != 'PASS' or score < int(settings.config['blog']['review_pass_score']):
                result.update(status='DROP_REVIEW', score=score, reason='editorial_revision_not_approved')
            else:
                post.quality_score, post.status = score, 'TEXT_APPROVED'
                existing = conn.execute('SELECT id FROM posts WHERE request_id=?', (post.request_id,)).fetchone()
                if existing:
                    post_id = existing[0]
                    with conn:
                        conn.execute('UPDATE posts SET subcategory=?,topic=?,title=?,body=?,tags_json=?, '
                                     'sources_json=?,quality_score=?,status=?,fingerprint=?,provenance_json=? WHERE id=?',
                                     (post.subcategory, post.topic, post.title, post.body, json.dumps(post.tags),
                                      json.dumps(post.source_urls), score, post.status, post.fingerprint,
                                      json.dumps(post.provenance), post_id))
                else:
                    post_id = save_post(conn, post)
                stem = settings.artifact_dir / f'{post.as_of_date}-{post_id:05d}.json'
                atomic_json(stem, {'post': asdict(post), 'input': asdict(request), 'review': review})
                result.update(complete_media(settings, conn, post_id, post, request, review))
            with conn:
                conn.execute('UPDATE attempts SET status=? WHERE id=?', (result['status'], row['id']))
        except (OpenAIError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            result.update(status='ERROR', stage='editorial_recovery', error=type(exc).__name__,
                          reason=getattr(exc, 'reason', 'editorial_recovery_failed'))
        results.append(result)
    return results
