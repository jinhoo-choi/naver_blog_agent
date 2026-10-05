"""Offline, bounded Creator Advisor evidence consulted before topic reservation.

The API preparer has no authenticated browser. Only an operator's observed UI
snapshot is imported; this module never fetches private endpoints or infers views.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from .core import max_title_similarity, recent_titles, today_kst
from .images import atomic_json

VERSION = 'creator-advisor-v1'
SNAPSHOT = 'creator-trends.json'
REPORT = 'creator-trend-selection.json'
DECISIONS = 'creator-trend-decisions.json'
MAX_BYTES = 40_000  # Fits one Actions variable, never a full page/body dump.
KST = ZoneInfo('Asia/Seoul')
UI_CATEGORIES = {
    'parenting': {'육아·결혼'},
    'exercise': {'스포츠', '건강·의학'},
    'investment': {'비즈니스·경제'},
    'origins': {'요리·레시피', '맛집', '국내여행', '해외여행', '문학·책'},
}
METRICS = {'keyword_inflow_order', 'search_inflow_rank', 'main_inflow_rank'}


def _text(value, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('Invalid Creator Advisor text')
    if any(ord(c) < 32 for c in value):
        raise ValueError('Control characters are not evidence')
    return value


def _url(value, *, article=False) -> str:
    value = _text(value, 1600)
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port:
        raise ValueError('Invalid Creator Advisor URL')
    if article:
        valid = (parsed.hostname in {'blog.naver.com', 'm.blog.naver.com'}
                 and re.fullmatch(r'/[A-Za-z0-9_.-]+/\d+/?', parsed.path)
                 and not parsed.query and not parsed.fragment)
    else:
        valid = (parsed.hostname == 'creator-advisor.naver.com'
                 and (re.fullmatch(r'/naver_blog/[A-Za-z0-9_.-]+/trends/?', parsed.path)
                      or parsed.path == '/new-windows/trend-stats'))
    if not article:
        query = parse_qs(parsed.query, keep_blank_values=True)
        valid = valid and set(query) <= {'startDate', 'endDate', 'interval', 'query', 'service'}
        valid = valid and all(len(values) == 1 for values in query.values())
        valid = valid and query.get('service', ['naver_blog']) == ['naver_blog']
        valid = valid and query.get('interval', ['day']) == ['day']
    if not valid:
        raise ValueError('Use the observed Creator Advisor or article URL')
    return value


def validate_snapshot(raw: str) -> dict:
    if len(raw.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Creator Advisor snapshot exceeds 40 KB')
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get('version') != VERSION:
        raise ValueError('Unsupported Creator Advisor snapshot')
    if payload.get('enabled', True) is not True:
        raise ValueError('Replace the example with observed evidence before enabling it')
    channel = _text(payload.get('channel_id'), 60)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', channel):
        raise ValueError('Invalid channel')
    captured = datetime.fromisoformat(_text(payload.get('captured_at'), 40))
    if captured.tzinfo is None:
        raise ValueError('Capture timestamp must include its timezone')
    if payload.get('capture_method') not in {'authenticated_ui', 'owner_screenshot'}:
        raise ValueError('Snapshot requires observed UI evidence')
    groups = payload.get('groups')
    if not isinstance(groups, list) or not 1 <= len(groups) <= 6:
        raise ValueError('Snapshot requires 1 to 6 observed groups')
    clean = []
    for group in groups:
        category = group['category']
        metric = group['metric']
        if category not in UI_CATEGORIES or metric not in METRICS:
            raise ValueError('Unsupported category or metric')
        ui_category = group['ui_category']
        if ui_category not in UI_CATEGORIES[category] | (
                {'전체'} if metric == 'main_inflow_rank' else set()):
            raise ValueError('Wrong UI category for this editorial category')
        day = date.fromisoformat(group['data_date'])
        if day > captured.astimezone(KST).date():
            raise ValueError('Data date is after observation')
        keyword = group.get('keyword', '')
        if metric == 'search_inflow_rank':
            _text(keyword, 80)
        elif keyword:
            raise ValueError('Only keyword-detail article groups have a keyword')
        source = _url(group['source_url'])
        source_path = urlsplit(source).path
        if ('/naver_blog/' in source_path
                and source_path.rstrip('/') != f'/naver_blog/{channel}/trends'):
            raise ValueError('Source channel does not match snapshot')
        entries = group['entries']
        if not isinstance(entries, list) or not 1 <= len(entries) <= 10:
            raise ValueError('Capture at most ten observed entries per group')
        normalized, ranks, urls = [], set(), set()
        for entry in entries:
            rank = entry['rank']
            if type(rank) is not int or not 1 <= rank <= 100 or rank in ranks:
                raise ValueError('Invalid or repeated displayed rank')
            url = _url(entry['url'], article=metric != 'keyword_inflow_order')
            if url in urls:
                raise ValueError('Repeated evidence URL')
            # The verified UI exposes inflow ranks, not per-article view counts.
            if entry.get('views') is not None:
                raise ValueError('Inflow rank/change is not a view count')
            normalized.append({'rank': rank, 'title': _text(entry['title'], 250),
                               'url': url, 'views': None})
            ranks.add(rank)
            urls.add(url)
        clean.append({'category': category, 'ui_category': ui_category, 'metric': metric,
                      'data_date': day.isoformat(), 'source_url': source, 'keyword': keyword,
                      'entries': sorted(normalized, key=lambda row: row['rank'])})
    return {'version': VERSION, 'channel_id': channel,
            'captured_at': captured.isoformat(), 'capture_method': payload['capture_method'],
            'groups': clean}


def import_snapshot(settings, source: Path) -> dict:
    """No model, browser, generation, queue, or schedule side effects."""
    with source.open('r', encoding='utf-8-sig') as stream:
        raw = stream.read(MAX_BYTES + 1)
    snapshot = validate_snapshot(raw)
    expected = settings.config.get('creator_advisor', {}).get('channel_id', '')
    if expected and snapshot['channel_id'] != expected:
        raise ValueError('Unexpected Creator Advisor channel')
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(settings.db_path.parent / SNAPSHOT, snapshot)
    return {'status': 'CREATOR_TRENDS_IMPORTED', 'groups': len(snapshot['groups']),
            'captured_at': snapshot['captured_at']}


def _load(settings) -> tuple[dict | None, str]:
    path = settings.db_path.parent / SNAPSHOT
    raw = os.environ.get('BLOG_CREATOR_TRENDS_JSON', '')
    try:
        if not raw:
            if not path.exists():
                return None, 'UNAVAILABLE'
            with path.open(encoding='utf-8') as stream:
                raw = stream.read(MAX_BYTES + 1)
        snapshot = validate_snapshot(raw)
        expected = settings.config['creator_advisor']['channel_id']
        if snapshot['channel_id'] != expected:
            return None, 'WRONG_CHANNEL'
        captured = datetime.fromisoformat(snapshot['captured_at'])
        age = (datetime.now(KST) - captured).total_seconds() / 3600
        if not 0 <= age <= 36:
            return None, 'EXPIRED' if age > 36 else 'FUTURE_CAPTURE'
        # Do not silently fall back to old evidence when the configured input is invalid.
        atomic_json(path, snapshot)
        return snapshot, 'AVAILABLE'
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None, 'INVALID'


def _public_keyword(request) -> str:
    word = request.data.get('benchmark_query') or (
        request.data.get('stock_name', '') if request.category == 'investment' else '')
    return word.strip() if isinstance(word, str) and 2 <= len(word.strip()) <= 80 else ''


def _matches(keyword: str, text: str) -> bool:
    # Whole keyword token sequence; generic partial-token overlaps do not imply relevance.
    words = re.findall(r'[가-힣a-z0-9]+', keyword.lower())
    haystack = re.findall(r'[가-힣a-z0-9]+', text.lower())
    return bool(words) and any(haystack[i:i + len(words)] == words
                              for i in range(len(haystack) - len(words) + 1))


def consult(settings, conn, candidates: list, daily_target: int) -> list:
    """Attach auditable evidence and rank only eligible, unattempted category peers."""
    config = settings.config.get('creator_advisor', {})
    if not config.get('enabled', False):
        return candidates
    snapshot, status = _load(settings)
    today = today_kst()
    directory = settings.db_path.parent
    used = {row[0] for row in conn.execute('SELECT request_id FROM attempts')}
    prior = {}
    try:
        saved = json.loads((directory / DECISIONS).read_text())
        if saved.get('date') == str(today) and isinstance(saved.get('decisions'), dict):
            prior = saved['decisions']
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    titles = recent_titles(conn)
    try:
        context = json.loads((directory / 'context.json').read_text())
        titles += [t for t in context.get('published_titles', []) if isinstance(t, str)]
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest() if snapshot else None
    output, replay_candidates, decisions = list(candidates), list(candidates), dict(prior)
    scheduled = settings.config.get('topics', {}).get('scheduled', {}).get(str(today), {}).get('id')
    for index, request in enumerate(candidates):
        if request.category not in UI_CATEGORIES:
            continue
        if request.id in used:
            # Preserve the exact pre-call provenance on retries; changed evidence is
            # never a reason to buy a replacement response or invalidate a cache.
            if request.id in prior:
                output[index] = replace(request, provenance={**request.provenance,
                                                            'creator_trends': prior[request.id]})
                replay_candidates[index] = output[index]
            continue
        keyword = _public_keyword(request)
        matches, category_seen, fresh_seen = [], False, False
        for group in snapshot['groups'] if snapshot else []:
            if group['category'] != request.category:
                continue
            category_seen = True
            data_age = (today - date.fromisoformat(group['data_date'])).days
            if not 0 <= data_age <= 3:
                continue
            fresh_seen = True
            for entry in group['entries']:
                relevant = keyword and (_matches(group['keyword'], keyword)
                    if group['metric'] == 'search_inflow_rank' else _matches(keyword, entry['title']))
                if relevant:
                    matches.append({**entry, **{key: group[key] for key in
                                    ('metric', 'ui_category', 'data_date', 'source_url', 'keyword')}})
        question = str(request.data.get('question') or request.data.get('title') or keyword)
        similarity = max_title_similarity(question, titles)
        duplicate = similarity >= float(settings.config['blog']['max_similarity'])
        articles = [row for row in matches if row['metric'] != 'keyword_inflow_order']
        state = ('CONSULTED' if articles else 'KEYWORDS_ONLY' if matches else
                 'NO_RELEVANT_MATCH' if fresh_seen else 'EXPIRED_DATA' if category_seen else
                 'CATEGORY_UNAVAILABLE' if snapshot else status)
        decision = {'version': VERSION, 'status': state, 'snapshot_sha256': digest,
                    'captured_at': snapshot['captured_at'] if snapshot else None,
                    'public_keyword': keyword, 'matches': (articles + [row for row in matches
                        if row['metric'] == 'keyword_inflow_order'])[:5],
                    'article_match_count': len(articles), 'duplicate_title_risk': duplicate,
                    'max_title_similarity': round(similarity, 4),
                    'priority_preserved': request.id == scheduled,
                    'basis': 'inflow_rank_not_views_factual_evidence_or_search_rank_prediction'}
        decisions[request.id] = decision
        output[index] = replace(request, provenance={**request.provenance, 'creator_trends': decision})
    # No new topics, category moves, retries, quota consumption, or paid requests.
    # A matched article is a demand clue only; title overlap never proves body duplication.
    interests = settings.config.get('community', {}).get('search_interest_keywords', [])
    for category in UI_CATEGORIES:
        slots = [i for i, r in enumerate(output) if r.category == category
                 and r.id not in used and r.id != scheduled][:5]
        def priority(index):
            r = output[index]
            d = r.provenance['creator_trends']
            return (r.category == 'investment' and r.data.get('kind') not in {'research', 'policy'},
                    r.category == 'investment' and r.data.get('stock_name') not in interests,
                    not (d['article_match_count'] and not d['duplicate_title_risk']))
        ordered = sorted(slots, key=priority)
        replacements = [output[i] for i in ordered]
        for slot, request in zip(slots, replacements):
            output[slot] = request
    # Only today's finite queue decisions are retained, including attempted identities.
    keep = used | {r.id for r in candidates}
    decisions = {key: value for key, value in decisions.items() if key in keep}
    try:
        atomic_json(directory / DECISIONS, {'date': str(today), 'decisions': decisions})
    except OSError:
        # No new prompt input unless it can be reused exactly after a paid attempt.
        held = [r.id for r in candidates if r.id in prior and r.id not in used]
        print(json.dumps({'status': 'CREATOR_TRENDS_RECORD_UNAVAILABLE',
                          'reason': 'decision_write_failed', 'held_request_ids': held}), flush=True)
        # An old unattempted sidecar must not be attached later to a new paid
        # request that ran without it. Hold only those ambiguous identities.
        return [r for r in replay_candidates if r.id not in held]
    report = {'date': str(today), 'version': VERSION, 'status': status,
                'source': 'authenticated_ui_snapshot', 'network_calls': 0, 'paid_calls': 0,
                'daily_target': daily_target, 'original_order': [r.id for r in candidates],
                'order': [r.id for r in output],
                'candidates': [{'id': r.id, **r.provenance.get('creator_trends',
                                {'status': 'RECOVERY_PROVENANCE_UNCHANGED'})} for r in output]}
    try:
        atomic_json(directory / REPORT, report)
    except OSError:
        print(json.dumps({'status': 'CREATOR_TRENDS_RECORD_UNAVAILABLE',
                          'reason': 'report_write_failed'}), flush=True)
    return output
