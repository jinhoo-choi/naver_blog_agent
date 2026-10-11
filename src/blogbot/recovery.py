"""Explicit, bounded editorial recovery of existing rejected responses."""
from __future__ import annotations

import hashlib
import json
import os
import re
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
from .llm import (
    BlogLLM,
    _cached_review_request,
    _checked_review,
    _historical_source_urls,
    _source_urls,
)
from .planning import (
    MediaBusy,
    SaveDateRequired,
    active_plan,
    attempt_matches,
    matches_post,
    matches_request,
    media_claim,
    reservation_result,
    saved_count,
)
from .presentation import normalize_structure, validate_structure
from .research import prepare_request

SOURCE_REVIEW_VERSION = 'source-review-once-v1'
ATTRIBUTION_REVIEW_VERSION = 'source-review-attribution-once-v1'


def source_review_hashes(post, request):
    """Operator manifest pins the normalized original text and original request input."""
    from .pre_review import candidate_from_post
    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'version': SOURCE_REVIEW_VERSION,
            'original_manuscript_sha256': digest(candidate_from_post(post).payload),
            'request_sha256': digest(request.prompt_data())}


def attribution_review_hashes(previous_claim: bytes, previous_input: bytes, corrected_post):
    """Bind one explicit attribution exception to immutable prior evidence and exact new text."""
    packet = json.loads(previous_claim)
    request = ContentRequest(**packet['input'])
    identity = source_review_hashes(corrected_post, request)
    return {'version': ATTRIBUTION_REVIEW_VERSION,
            'request_sha256': identity['request_sha256'],
            'previous_claim_sha256': hashlib.sha256(previous_claim).hexdigest(),
            'previous_input_sha256': hashlib.sha256(previous_input).hexdigest(),
            'previous_review_sha256': hashlib.sha256(json.dumps(
                packet['review'], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'corrected_manuscript_sha256': identity['original_manuscript_sha256']}


def _attribution_correction(post, replacement):
    """Replace one approved parenthesis in the final reference section, changing nothing else."""
    if not isinstance(replacement, dict) or set(replacement) != {'old', 'new'}:
        raise ValueError('Attribution correction requires one exact replacement')
    old, new = replacement['old'], replacement['new']
    if (any(not isinstance(value, str) or not re.fullmatch(r'\([^()\r\n]+\)', value)
            for value in (old, new))
            or old == new or post.body.count(old) != 1):
        raise ValueError('Attribution correction must change one parenthetical clause')
    sections = list(re.finditer(r'^#{2,3}\s*(?:참고\s*자료|참고\s*문헌|출처)\s*$',
                                post.body, re.MULTILINE))
    if not sections or old not in post.body[sections[-1].end():]:
        raise ValueError('Attribution correction must stay in the final reference section')
    if re.search(r'^#{2,3}\s', post.body[sections[-1].end():], re.MULTILINE):
        raise ValueError('Attribution correction reference section must be last')
    corrected = post.body.replace(old, new, 1)
    urls = r'https?://[^\s<>\]\)]+'
    if re.findall(urls, post.body, re.IGNORECASE) != re.findall(urls, corrected, re.IGNORECASE):
        raise ValueError('Attribution correction cannot change body source URLs')
    return replace(post, body=corrected)


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
    observed = _historical_source_urls(directory / 'usage.jsonl', request.id)
    if request.provenance.get('investment_mode') == 'life-economics-v1':
        observed = [s['url'] for s in request.provenance.get('life_economics_checks', [])
                    if s.get('verified_date') == str(today_kst())]
    if raw:
        from .pre_review import DraftCandidate
        return DraftCandidate(data, observed)
    reviewer = cached.get('reviewer', {})
    sources = _source_urls(data, observed)
    post = PostDraft(request.category, data['subcategory'], original['payload']['title'],
                     data['title'], data['body'], data['tags'], sources, str(today_kst()),
                     request_id=request.id, photos=request.photos, provenance=request.provenance)
    reviewed_latest = positions.get('reviewer', -1) > max(
        positions.get('writer', -1), positions.get('rewrite', -1))
    return {'post': asdict(post), 'input': asdict(request),
            'raw_review': reviewer.get('payload', {}) if reviewed_latest else {},
            'review': _checked_review(reviewer.get('payload', {}), reviewer.get('response', {}),
                                      _cached_review_request(request, reviewer))
            if reviewed_latest else {}}


def _editorial_patch_data(settings, post):
    key = hashlib.sha256(post.request_id.encode()).hexdigest()
    path = settings.root / 'editorial' / post.as_of_date / f'{key}.enc'
    if not path.exists():
        return {}
    try:
        data = json.loads(Fernet(os.environ['BLOG_BUNDLE_KEY']).decrypt(path.read_bytes()))
    except InvalidToken as exc:
        raise ValueError('Editorial correction decryption failed') from exc
    if (set(data) - {'request_id', 'as_of_date', 'title', 'body', 'source_urls', 'review_recovery'}
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
    return data


def editorial_patch(settings, post):
    """Apply an explicit dated operator correction; never confer approval."""
    data = _editorial_patch_data(settings, post)
    if data.get('review_recovery'):
        raise ValueError('Source review authorization requires its bounded recovery path')
    return replace(post, **{k: data[k] for k in ('title', 'body', 'source_urls') if k in data})


def _finish_recovered_post(settings, conn, post, request, review):
    from .pipeline import complete_media
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    post.quality_score, post.status = review_result(review)[0], 'TEXT_APPROVED'
    existing = conn.execute('SELECT id FROM posts WHERE request_id=?', (post.request_id,)).fetchone()
    if existing:
        post_id = existing[0]
        with conn:
            conn.execute('UPDATE posts SET subcategory=?,topic=?,title=?,body=?,tags_json=?, '
                         'sources_json=?,quality_score=?,status=?,fingerprint=?,provenance_json=? WHERE id=?',
                         (post.subcategory, post.topic, post.title, post.body, json.dumps(post.tags),
                          json.dumps(post.source_urls), post.quality_score, post.status, post.fingerprint,
                          json.dumps(post.provenance), post_id))
    else:
        post_id = save_post(conn, post)
    stem = settings.artifact_dir / f'{post.as_of_date}-{post_id:05d}.json'
    atomic_json(stem, {'post': asdict(post), 'input': asdict(request), 'review': review})
    return complete_media(settings, conn, post_id, post, request, review)


def _recover_source_review(settings, conn, row, candidate, plan):
    """Explicit encrypted opt-in: one reviewer, unchanged quota, never another rewrite."""
    try:
        # Serialize this opt-in path across all request IDs for the same daily slot.
        # The durable packet, not the process lock, remains the paid-call boundary.
        with media_claim(settings.db_path.parent, 'source-review:' + str(today_kst())):
            status = conn.execute('SELECT status FROM attempts WHERE id=?', (row['id'],)).fetchone()
            if status is None or status[0] != 'DROP_REVIEW':
                return {'status': status[0] if status else 'MANUAL_CHECK_REQUIRED'}
            return _recover_source_review_locked(settings, conn, row, candidate, plan)
    except MediaBusy:
        return {'status': 'MANUAL_CHECK_REQUIRED', 'reason': 'source_review_in_progress'}


def _recover_source_review_locked(settings, conn, row, candidate, plan):
    from .pre_review import DraftCandidate, checkpoint_path, inspect_candidate
    directory = settings.db_path.parent
    checkpoint = json.loads(checkpoint_path(directory, row['request_id']).read_text())
    request = replace(candidate, provenance=checkpoint['request']['provenance'])
    if request.prompt_data() != checkpoint['request']:
        raise ValueError('Source review original input changed')
    info = settings.config['categories'][request.category]
    latest = rejected_checkpoint(settings, request)
    raw = (DraftCandidate(**checkpoint['corrected']) if checkpoint.get('correction_attempted')
           else rejected_checkpoint(settings, request, raw=True))
    original, issues, _ = inspect_candidate(
        raw, request, info, settings.config.get('editorial', {}).get('require_structure', True))
    if issues:
        raise ValueError('Source review original manuscript is invalid')
    patch = _editorial_patch_data(settings, original)
    authorization = patch.get('review_recovery')
    if authorization is None:
        return None
    if authorization != source_review_hashes(original, request):
        raise ValueError('Source review authorization identity mismatch')
    if (request.id != row['request_id'] or request.category != row['category']
            or original.as_of_date != str(today_kst())
            or (plan and (not matches_request(request, plan) or not matches_post(original, plan)
                          or request.provenance.get('daily_plan') != plan))):
        raise ValueError('Source review request or plan mismatch')
    post = replace(original, **{k: patch[k] for k in ('title', 'body', 'source_urls') if k in patch})
    from .pre_review import candidate_from_post
    post, issues, _ = inspect_candidate(
        candidate_from_post(post), request, info,
        settings.config.get('editorial', {}).get('require_structure', True))
    if issues:
        raise ValueError('Source review correction is invalid')
    if conn.execute("SELECT 1 FROM posts WHERE status IN ('SAVING','SAVE_UNCERTAIN') LIMIT 1").fetchone():
        return {'status': 'MANUAL_CHECK_REQUIRED', 'reason': 'save_outcome_requires_check'}
    if plan:
        try:
            if saved_count(conn, plan):
                return {'status': 'DAILY_PLAN_LIMIT', 'reason': 'source_review_daily_limit'}
        except SaveDateRequired:
            return {'status': 'MANUAL_CHECK_REQUIRED', 'reason': 'save_date_reconciliation_required'}
    occupied = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE as_of_date=? AND request_id!=? AND status IN "
        "('APPROVED','TEXT_APPROVED','IMAGES_PENDING','SAVED_NAVER','SAVING','SAVE_UNCERTAIN')",
        (str(today_kst()), request.id)).fetchone()[0]
    if occupied >= int(settings.config['blog']['daily_max']):
        return {'status': 'DAILY_PLAN_LIMIT', 'reason': 'source_review_daily_limit'}
    path = checkpoint_path(directory, request.id).with_suffix('.source-review.json')
    identity = {'authorization': authorization, 'post': asdict(post), 'input': asdict(request)}
    if path.exists():
        packet = json.loads(path.read_text())
        if any(packet.get(k) != v for k, v in identity.items()):
            raise ValueError('Source review correction changed after claim')
        resumed = True
    else:
        # Only source-reading failure after an otherwise passing formal review qualifies.
        # A new evidence fetch must never reinterpret that old response as a new review.
        raw_score, raw_decision = review_result(latest['raw_review'])
        if (raw_decision != 'PASS' or raw_score < int(settings.config['blog']['review_pass_score'])
                or latest['review'].get('blocking_issues') != [
                    '핵심 주장별 원문 열람·근거 대조가 완료되지 않았습니다.']):
            raise ValueError('Source review recovery requires an isolated source-reading failure')
        packet = identity
        try:
            with path.open('x', encoding='utf-8') as stream:
                json.dump(packet, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            resumed = False
        except FileExistsError:
            raise ValueError('Source review recovery already claimed') from None
    if 'review' in packet:
        review = packet['review']
    else:
        llm = BlogLLM(settings.openai_api_key, settings.openai_model, settings.root,
                      settings.review_model, directory / 'usage.jsonl')
        review = llm.review(post, info, request, cache_only=resumed, single_attempt=True)
        atomic_json(path, {**packet, 'review': review})
    score, decision = review_result(review)
    result = {'status': 'DROP_REVIEW', 'score': score, 'reason': 'source_review_not_approved'}
    other_titles = [r[0] for r in conn.execute(
        'SELECT title FROM posts WHERE request_id!=?', (request.id,))]
    if max_title_similarity(post.title, other_titles) >= float(settings.config['blog']['max_similarity']):
        result['reason'] = 'duplicate_title'
    elif decision == 'PASS' and score >= int(settings.config['blog']['review_pass_score']):
        existing = conn.execute('SELECT status FROM posts WHERE request_id=?', (request.id,)).fetchone()
        if existing and existing[0] in {'APPROVED', 'SAVED_NAVER', 'TEXT_APPROVED', 'IMAGES_PENDING'}:
            # A DB/media checkpoint may have completed before the attempt update.
            # Leave any media resume to its existing identity-bound path.
            result = {'status': existing[0], 'reason': 'source_review_post_already_prepared'}
        else:
            result = _finish_recovered_post(settings, conn, post, request, review)
    with conn:
        conn.execute('UPDATE attempts SET status=? WHERE id=?', (result['status'], row['id']))
    return result


def recover_attribution_review(settings, conn, request_id):
    """Explicit recover-only target; never enumerate candidates or grant a third review."""
    if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', request_id):
        raise ValueError('Attribution review requires one valid existing request ID')
    result = {'request_id': request_id, 'stage': 'source_review_attribution'}
    try:
        plan = active_plan(settings)
        if plan and plan.get('reservation'):
            return {**result, **reservation_result(plan)}
        with media_claim(settings.db_path.parent, 'source-review:' + str(today_kst())):
            return {**result, **_recover_attribution_locked(settings, conn, request_id, plan)}
    except MediaBusy:
        return {**result, 'status': 'MANUAL_CHECK_REQUIRED', 'reason': 'source_review_in_progress'}
    except (OpenAIError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        return {**result, 'status': 'ERROR', 'error': type(exc).__name__,
                'reason': getattr(exc, 'reason', 'attribution_review_recovery_failed')}


def _recover_attribution_locked(settings, conn, request_id, plan):
    from .core import load_post
    from .editorial import routed_category_info
    from .pipeline import complete_media
    from .pre_review import candidate_from_post, checkpoint_path, inspect_candidate
    directory = settings.db_path.parent
    rows = conn.execute('SELECT * FROM attempts WHERE request_id=? AND day=?',
                        (request_id, str(today_kst()))).fetchall()
    if len(rows) != 1:
        raise ValueError('Attribution review target is unknown or stale')
    row = rows[0]
    if row['status'] not in {'DROP_REVIEW', 'TEXT_APPROVED', 'IMAGES_PENDING', 'APPROVED', 'SAVED_NAVER'}:
        raise ValueError('Attribution review target is not a completed review')
    if plan and not attempt_matches(row, plan):
        raise ValueError('Attribution review attempt plan mismatch')
    checkpoint = checkpoint_path(directory, request_id)
    previous_claim = checkpoint.with_suffix('.source-review.json').read_bytes()
    previous_input = checkpoint.with_suffix('.source-review-input.json').read_bytes()
    previous = json.loads(previous_claim)
    frozen = json.loads(previous_input)
    request, original = ContentRequest(**previous['input']), PostDraft(**previous['post'])
    info = settings.config['categories'][request.category]
    if (request.id != request_id or original.request_id != request_id
            or request.category != row['category'] or original.category != request.category
            or original.as_of_date != str(today_kst()) or info['max_daily'] == 0
            or json.loads(checkpoint.read_text())['request'] != request.prompt_data()
            or previous['authorization'].get('version') != SOURCE_REVIEW_VERSION
            or (plan and (not matches_request(request, plan) or not matches_post(original, plan)
                          or request.provenance.get('daily_plan') != plan))):
        raise ValueError('Attribution review predecessor identity mismatch')
    score, decision = review_result(previous['review'])
    if (decision == 'PASS' or score < int(settings.config['blog']['review_pass_score'])
            or previous['review']['scores'][0] < 4 or previous['review']['scores'][5] < 4
            or previous['review'].get('blocking_issues') != [
                '핵심 주장별 원문 열람·근거 대조가 완료되지 않았습니다.']):
        raise ValueError('Attribution review requires a completed non-passing source review')
    routed = routed_category_info(info, request.category, request.data.get('editorial_type'),
                                  request.data.get('content_style'),
                                  investment_mode=request.provenance.get('investment_mode'))
    expected_input = hashlib.sha256(json.dumps(
        [settings.review_model or settings.openai_model, BlogLLM._draft_data(original),
         routed, request.prompt_data()],
        sort_keys=True).encode()).hexdigest()
    if (frozen.get('identity') != expected_input or not frozen.get('prompt')
            or frozen.get('context', {}).get('request_id') != request_id
            or frozen.get('context', {}).get('category') != request.category):
        raise ValueError('Attribution review predecessor input mismatch')
    key = hashlib.sha256(request_id.encode()).hexdigest()
    manifest_path = (settings.root / 'editorial' / str(today_kst())
                     / f'{key}-source-review-attribution.enc')
    try:
        manifest = json.loads(Fernet(os.environ['BLOG_BUNDLE_KEY']).decrypt(manifest_path.read_bytes()))
    except InvalidToken as exc:
        raise ValueError('Attribution authorization decryption failed') from exc
    if (set(manifest) != {'request_id', 'as_of_date', 'replacement', 'review_attribution'}
            or manifest['request_id'] != request_id or manifest['as_of_date'] != str(today_kst())):
        raise ValueError('Attribution authorization identity mismatch')
    corrected = _attribution_correction(original, manifest['replacement'])
    post, issues, _ = inspect_candidate(
        candidate_from_post(corrected), request, info,
        settings.config.get('editorial', {}).get('require_structure', True))
    if issues or post is None:
        raise ValueError('Attribution correction failed manuscript checks')
    if candidate_from_post(post).payload != candidate_from_post(corrected).payload:
        raise ValueError('Attribution correction would change other manuscript content')
    # Preserve topic, provenance and other non-manuscript fields from the first packet.
    post = corrected
    authorization = attribution_review_hashes(previous_claim, previous_input, post)
    if manifest['review_attribution'] != authorization:
        raise ValueError('Attribution authorization hashes do not match')
    if conn.execute("SELECT 1 FROM posts WHERE status IN ('SAVING','SAVE_UNCERTAIN') LIMIT 1").fetchone():
        return {'status': 'MANUAL_CHECK_REQUIRED', 'reason': 'save_outcome_requires_check'}
    if plan:
        try:
            if saved_count(conn, plan):
                return {'status': 'DAILY_PLAN_LIMIT', 'reason': 'source_review_daily_limit'}
        except SaveDateRequired:
            return {'status': 'MANUAL_CHECK_REQUIRED', 'reason': 'save_date_reconciliation_required'}
    occupied = conn.execute(
        "SELECT COUNT(*) FROM posts WHERE as_of_date=? AND request_id!=? AND status IN "
        "('APPROVED','TEXT_APPROVED','IMAGES_PENDING','SAVED_NAVER','SAVING','SAVE_UNCERTAIN')",
        (str(today_kst()), request_id)).fetchone()[0]
    if occupied >= int(settings.config['blog']['daily_max']):
        return {'status': 'DAILY_PLAN_LIMIT', 'reason': 'source_review_daily_limit'}
    path = checkpoint.with_suffix('.source-review-attribution.json')
    identity = {'authorization': authorization, 'replacement': manifest['replacement'],
                'post': asdict(post), 'input': asdict(request)}
    if path.exists():
        packet = json.loads(path.read_text())
        if any(packet.get(k) != value for k, value in identity.items()):
            raise ValueError('Attribution review changed after claim')
        resumed = True
    else:
        if row['status'] != 'DROP_REVIEW' or conn.execute(
                'SELECT 1 FROM posts WHERE request_id=?', (request_id,)).fetchone():
            raise ValueError('Attribution review cannot replace an existing prepared post')
        packet = identity
        with path.open('x', encoding='utf-8') as stream:
            json.dump(packet, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        resumed = False
    if 'review' in packet:
        review = packet['review']
    else:
        llm = BlogLLM(settings.openai_api_key, settings.openai_model, settings.root,
                      settings.review_model, directory / 'usage.jsonl')
        review = llm.review(post, info, request, cache_only=resumed, single_attempt=True,
                            attribution_review=True)
        atomic_json(path, {**packet, 'review': review})
    score, decision = review_result(review)
    result = {'status': 'DROP_REVIEW', 'score': score, 'reason': 'attribution_review_not_approved'}
    other_titles = [item[0] for item in conn.execute(
        'SELECT title FROM posts WHERE request_id!=?', (request_id,))]
    if max_title_similarity(post.title, other_titles) >= float(settings.config['blog']['max_similarity']):
        result['reason'] = 'duplicate_title'
    elif decision == 'PASS' and score >= int(settings.config['blog']['review_pass_score']):
        existing = conn.execute('SELECT * FROM posts WHERE request_id=?', (request_id,)).fetchone()
        if existing:
            ready = load_post(existing)
            if (ready.fingerprint != post.fingerprint or ready.request_id != request_id
                    or candidate_from_post(ready).payload != candidate_from_post(post).payload):
                raise ValueError('Attribution media checkpoint identity mismatch')
            if ready.status in {'APPROVED', 'SAVED_NAVER'}:
                result = {'status': ready.status, 'post_id': existing['id']}
            elif ready.status in {'TEXT_APPROVED', 'IMAGES_PENDING'}:
                result = complete_media(settings, conn, existing['id'], ready, request, review)
            else:
                raise ValueError('Attribution media checkpoint status requires reconciliation')
        else:
            result = _finish_recovered_post(settings, conn, post, request, review)
    with conn:
        conn.execute('UPDATE attempts SET status=? WHERE id=?', (result['status'], row['id']))
    return result


def recover_rejected(settings, conn, candidates):
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
            # Only a separate identity-bound operator manifest can buy one review,
            # never a rewrite or another automatic recovery allowance.
            guarded = {'status': 'DROP_REVIEW',
                       'reason': ('manuscript_correction_limit' if revision_path(
                           settings.db_path.parent, row['request_id']).exists()
                                  else 'review_rejected_after_pre_review')}
            key = hashlib.sha256(row['request_id'].encode()).hexdigest()
            patch_path = settings.root / 'editorial' / str(today_kst()) / f'{key}.enc'
            if patch_path.exists() and row['request_id'] in by_id:
                try:
                    guarded = _recover_source_review(
                        settings, conn, row, by_id[row['request_id']], plan) or guarded
                except (OpenAIError, OSError, RuntimeError, ValueError, TypeError, KeyError,
                        sqlite3.Error) as exc:
                    guarded = {'status': 'ERROR', 'stage': 'source_review_recovery',
                               'error': type(exc).__name__,
                               'reason': getattr(exc, 'reason', 'source_review_recovery_failed')}
            results.append({**result, **guarded})
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
            from .investment import refresh_request
            request = refresh_request(settings, request)
            post = PostDraft(**payload['post'])
            if (request.id != row['request_id'] or post.request_id != request.id
                    or request.category != category or post.category != category
                    or (plan and (not matches_request(request, plan) or not matches_post(post, plan)
                                  or request.provenance.get('daily_plan') != plan))):
                raise ValueError('Recovery packet identity or plan mismatch')
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
                result.update(_finish_recovered_post(settings, conn, post, request, review))
            with conn:
                conn.execute('UPDATE attempts SET status=? WHERE id=?', (result['status'], row['id']))
        except (OpenAIError, OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            result.update(status='ERROR', stage='editorial_recovery', error=type(exc).__name__,
                          reason=getattr(exc, 'reason', 'editorial_recovery_failed'))
        results.append(result)
    return results
