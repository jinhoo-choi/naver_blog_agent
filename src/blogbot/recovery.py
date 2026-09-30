"""Explicit, bounded editorial recovery of existing rejected responses."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict

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
from .llm import BlogLLM, _extract_urls, _source_urls
from .presentation import normalize_structure, validate_structure
from .research import prepare_request


def rejected_checkpoint(settings, request):
    directory = settings.db_path.parent
    ids = {}
    for line in (directory / 'usage.jsonl').read_text().splitlines():
        entry = json.loads(line)
        if entry['request_id'] == request.id and not entry.get('error') and entry.get('response_id'):
            ids[entry['stage']] = entry['response_id']
    cached = {}
    for path in (directory / 'response-cache').glob(f'{today_kst()}-*.json'):
        item = json.loads(path.read_text())
        for stage, response_id in ids.items():
            if item.get('response', {}).get('id') == response_id:
                cached[stage] = item
    original = cached['writer']
    latest = cached.get('rewrite', original)
    data = latest['payload']
    sources = _source_urls(data, _extract_urls(original['response']) + _extract_urls(latest['response']))
    post = PostDraft(request.category, data['subcategory'], original['payload']['title'],
                     data['title'], data['body'], data['tags'], sources, str(today_kst()),
                     request_id=request.id, photos=request.photos, provenance=request.provenance)
    return {'post': asdict(post), 'input': asdict(request), 'review': cached['reviewer']['payload']}


def recover_rejected(settings, conn, candidates):
    from .pipeline import complete_media
    results, handled = [], set()
    by_id = {request.id: request for request in candidates}
    ready = {row[0] for row in conn.execute(
        "SELECT category FROM posts WHERE as_of_date=? AND status IN "
        "('APPROVED','TEXT_APPROVED','IMAGES_PENDING','SAVED_NAVER')", (str(today_kst()),))}
    for row in conn.execute("SELECT * FROM attempts WHERE day=? AND status='DROP_REVIEW' ORDER BY id",
                            (str(today_kst()),)).fetchall():
        category = row['category']
        if category in ready or category in handled:
            continue
        handled.add(category)  # At most one existing manuscript per missing category.
        result = {'attempt': row['id'], 'category': category, 'request_id': row['request_id']}
        key = hashlib.sha256(row['request_id'].encode()).hexdigest()
        path = settings.db_path.parent / 'response-cache' / f'{today_kst()}-recovery-{key}.json'
        try:
            if path.exists():
                payload = json.loads(path.read_text())
            else:
                request = prepare_request(settings, by_id[row['request_id']])
                payload = rejected_checkpoint(settings, request)
            request = ContentRequest(**payload['input'])
            post = PostDraft(**payload['post'])
            info = settings.config['categories'][category]
            validate_post(post, info)
            resumed = bool(payload.get('revision_attempted'))
            atomic_json(path, {**payload, 'revision_attempted': True})
            llm = BlogLLM(settings.openai_api_key, settings.openai_model, settings.root,
                          settings.review_model, settings.db_path.parent / 'usage.jsonl')
            post = llm.rewrite(post, info, payload['review'], request, cache_only=resumed)
            post = normalize_structure(post)
            validate_post(post, info)
            validate_structure(post, settings.config.get('editorial', {}).get('require_structure', True))
            review = llm.review(post, info, request)
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
