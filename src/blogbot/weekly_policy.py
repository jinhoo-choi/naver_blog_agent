"""Fail-closed weekly policy evidence layered onto scored, immutable KIS exports.

A registered bundle is evidence, never a candidate or an approval. Network reads
are bounded, anonymous, HTTPS-only and restricted to configured official hosts.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import date
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import core

VERSION = 'weekly-policy-v1'
TIERS = {'official_participation', 'previous_project', 'business_relevance'}


def now_kst():
    return core.datetime.now(core.KST)


def enabled(settings):
    plan = getattr(settings, 'config', {}).get('daily_plan') or {}
    return plan.get('investment_mode') == VERSION


def record_digest(record):
    return hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def source_identity(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or not p.hostname or p.username or p.password
            or p.port not in (None, 443) or len(url) > 2000):
        raise ValueError('Invalid policy source URL')
    return urlunsplit((p.scheme, p.hostname.lower(), p.path, p.query, ''))


def _text(value, limit=2000):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError('Missing or oversized policy evidence text')
    return value.strip()


def _source(value, config):
    if not isinstance(value, dict):
        raise TypeError('Missing policy source evidence')
    url = source_identity(value['url'])
    host = urlsplit(url).hostname
    if not (host.endswith('.go.kr') or host in config.get('official_hosts', [])):
        raise ValueError('Policy evidence requires an approved official host')
    if urlsplit(url).path in ('', '/') and not urlsplit(url).query:
        raise ValueError('Policy source needs a specific official document')
    published = date.fromisoformat(value['published_date'])
    if published > now_kst().date():
        raise ValueError('Future publication is not current evidence')
    return {'url': url, 'published_date': str(published),
            'excerpt': _text(value['excerpt']),
            'date_excerpt': _text(value['date_excerpt'], 300),
            **({'export_excerpt': _text(value['export_excerpt'])} if value.get('export_excerpt') else {})}


def _date_present(day, text):
    d = date.fromisoformat(day)
    return bool(re.search(rf'{d.year}\s*(?:[-./]|년)\s*0?{d.month}\s*(?:[-./]|월)\s*0?{d.day}(?:일|\b)', text)
                or d.strftime('%Y%m%d') in text)


def validate_bundle(raw, config):
    if not isinstance(raw, dict) or raw.get('version') != VERSION:
        raise ValueError('Unsupported policy evidence version')
    source = raw['source']
    if (not isinstance(source, dict) or source.get('repository') != config['repository']
            or not re.fullmatch(r'[a-f0-9]{40}', source.get('commit', ''))
            or not re.fullmatch(r'[a-f0-9]{64}', source.get('record_sha256', ''))):
        raise ValueError('Invalid policy export binding')
    binding = {k: source[k] for k in ('repository', 'commit', 'record_id', 'record_sha256')}
    binding['record_id'] = _text(binding['record_id'], 300)
    binding['url'] = source_identity(source['url'])
    event = raw['event']
    day = date.fromisoformat(event['date'])
    if not 0 <= (now_kst().date() - day).days <= 4:
        raise ValueError('Policy event outside five-calendar-day window')
    if event.get('novelty') not in {'new_announcement', 'new_step'} or event.get('stage') not in {
            'proposed', 'announced', 'adopted', 'effective'}:
        raise ValueError('Policy needs explicit novelty and stage')
    official = _source(event['source'], config)
    # A fresh rereport about an old event and a future effective date cannot refresh it.
    if official['published_date'] != str(day) or not _date_present(str(day), official['date_excerpt']):
        raise ValueError('Policy event needs same-day official publication evidence')
    event_data = {'date': str(day), 'novelty': event['novelty'], 'stage': event['stage'],
                  'change': _text(event['change']), 'export_excerpt': _text(event['export_excerpt']),
                  'source': official}
    background = raw.get('background_sources', [])
    if not isinstance(background, list) or len(background) > 3:
        raise ValueError('Invalid policy background sources')
    backgrounds = [_source(s, config) for s in background]
    if any(s['published_date'] >= str(day) for s in backgrounds):
        raise ValueError('Background must predate the new event')
    if event['novelty'] == 'new_step' and not backgrounds:
        raise ValueError('A new step requires dated earlier background')
    companies = raw['company_links']
    if not isinstance(companies, list) or not 1 <= len(companies) <= 3:
        raise ValueError('Policy requires one to three evidenced company relationships')
    links = []
    for link in companies:
        if link.get('relationship') not in TIERS:
            raise ValueError('Unsupported company relationship tier')
        company = _text(link['company'], 100)
        evidence = _source(link['source'], config)
        if company not in evidence['excerpt']:
            raise ValueError('Company relationship excerpt must name the company')
        if link['relationship'] == 'official_participation' and evidence['published_date'] < str(day):
            raise ValueError('Earlier company evidence cannot establish new official participation')
        links.append({'company': company, 'relationship': link['relationship'],
                      'claim': _text(link['claim']), 'source': evidence})
    milestones = raw['milestones']
    if not isinstance(milestones, list) or not 1 <= len(milestones) <= 3:
        raise ValueError('Policy requires future milestones or explicit conditions')
    future = []
    for milestone in milestones:
        due = milestone.get('date')
        if due is not None and date.fromisoformat(due) < day:
            raise ValueError('Milestone precedes the policy event')
        evidence = _source(milestone['source'], config)
        if due is not None and not _date_present(due, evidence['excerpt']):
            raise ValueError('A dated milestone requires the exact date in its evidence')
        future.append({'date': due, 'condition': _text(milestone['condition']),
                       'source': evidence})
    return {'version': VERSION, 'source': binding, 'event': event_data,
            'background_sources': backgrounds, 'company_links': links, 'milestones': future}


def bundle_path(settings, commit, record_id):
    key = hashlib.sha256(f'{commit}:{record_id}'.encode()).hexdigest()
    return settings.db_path.parent / 'policy-evidence' / f'{key}.json'


def import_evidence(settings, path):
    if path.stat().st_size > 100_000:
        raise ValueError('Policy bundle exceeds size limit')
    bundle = validate_bundle(json.loads(path.read_text(encoding='utf-8')),
                             {**settings.config['community'],
                              **settings.config['community'].get('weekly_policy', {})})
    return _register(settings, bundle)


def _register(settings, bundle):
    source = bundle['source']
    target = bundle_path(settings, source['commit'], source['record_id'])
    if target.exists() and json.loads(target.read_text()) != bundle:
        raise ValueError('Conflicting policy evidence; reconcile the existing bundle')
    from .images import atomic_json
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(target, bundle)
    return {'status': 'POLICY_EVIDENCE_REGISTERED', 'record_id': source['record_id'],
            'candidate_created': False}


def evidence_for(settings, record, origin):
    path = bundle_path(settings, origin['commit'], record['id'])
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 100_000:
        raise ValueError('Policy evidence bundle missing or invalid')
    bundle = validate_bundle(json.loads(path.read_text()),
                             {**settings.config['community'],
                              **settings.config['community'].get('weekly_policy', {})})
    expected = {'repository': origin['repository'], 'commit': origin['commit'],
                'record_id': record['id'], 'record_sha256': record_digest(record),
                'url': source_identity(record['src'])}
    if bundle['source'] != expected:
        raise ValueError('Policy evidence does not match the scored export record')
    # Every old date label must have explicitly dated background, never market data.
    dates = re.findall(r'(?m)^(?:발간일|발행일|공시일|접수일|보도\s*시각|보도일|자료\s*기준일|기준일)'
                       r'[ \t]*[:：]?[ \t]*(20\d{2})-?(\d{2})-?(\d{2})\b', record['facts'])
    allowed = {bundle['event']['date'], *(s['published_date'] for s in bundle['background_sources'])}
    if (bundle['event']['export_excerpt'] not in record['facts']
            or bundle['event']['date'] not in {'-'.join(d) for d in dates}
            or any(not s.get('export_excerpt') or s['export_excerpt'] not in record['facts']
                   or not _date_present(s['published_date'], s['export_excerpt'])
                   for s in bundle['background_sources'])):
        raise ValueError('New event and background must match exact export facts')
    if not dates or any('-'.join(d) not in allowed for d in dates):
        raise ValueError('Unclassified policy fact date')
    market_lines = [line for line in record['facts'].splitlines()
                    if re.search(r'주가|종가|시가|수급|순매수|순매도|거래량|거래대금', line)]
    if market_lines:
        market_date = record.get('market_as_of_date')
        if not isinstance(market_date, str) or not 0 <= (now_kst().date() - date.fromisoformat(market_date)).days <= int(settings.config['community'].get('max_age_days', 1)):
            raise ValueError('Market data retains the one-day freshness gate')
        for line in market_lines:
            dates = re.findall(r'(20\d{2})\s*(?:[-./]|년)?\s*(\d{1,2})\s*(?:[-./]|월)?\s*(\d{1,2})(?:일|\b)', line)
            if any(not 0 <= (now_kst().date() - date(int(y), int(m), int(d))).days <= 1
                   for y, m, d in dates):
                raise ValueError('Old or future market facts cannot be background')
    return bundle


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_official(url):
    from .research import _DisclosureText
    with build_opener(_NoRedirect()).open(Request(url, headers={
            'User-Agent': 'naver-blog-agent/0.5'}), timeout=25) as response:
        if 'text/html' not in response.headers.get('Content-Type', ''):
            raise ValueError('Policy evidence currently requires readable official HTML')
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Policy source exceeded size limit')
    parser = _DisclosureText()
    parser.feed(raw.decode('utf-8'))
    return ' '.join(' '.join(parser.parts).split())


def verify_evidence(settings, request):
    """Fresh official reads before paid preparation; metadata alone is never proof."""
    from .research import ResearchRequired
    if request.provenance.get('investment_mode') != VERSION:
        return request
    try:
        bundle = validate_bundle(request.data['policy_evidence'],
                                 {**settings.config['community'],
                                  **settings.config['community'].get('weekly_policy', {})})
        sources = [bundle['event']['source'], *bundle['background_sources'],
                   *(s['source'] for s in bundle['company_links']),
                   *(s['source'] for s in bundle['milestones'])]
        read, verified = {}, []
        for source in sources:
            url = source['url']
            if url not in read:
                read[url] = _read_official(url)
            if not all(' '.join(source[k].split()) in read[url] for k in ('excerpt', 'date_excerpt')):
                raise ValueError('Official source changed or does not support supplied excerpt')
            if not _date_present(source['published_date'], source['date_excerpt']):
                raise ValueError('Publication date is not supported by official source')
            verified.append({**source, 'verified_date': str(now_kst().date()),
                             'sha256': hashlib.sha256(read[url].encode()).hexdigest()})
        return replace(request, provenance={**request.provenance,
                       'policy_source_checks': verified})
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ResearchRequired('Weekly policy official-source verification required') from exc


def post_contract(post, plan):
    """Cheap transport/replay check; not a substitute for source revalidation."""
    try:
        p = post.provenance
        if p.get('investment_mode') != VERSION or plan.get('investment_mode') != VERSION:
            return False
        bundle = p['policy_evidence']
        binding = bundle['source']
        event = bundle['event']
        key = hashlib.sha256((source_identity(event['source']['url']) + ':'
                              + event['date']).encode()).hexdigest()
        expected_id = 'community-' + hashlib.sha256(
            f"{binding['repository']}:{binding['record_id']}".encode()).hexdigest()[:24]
        if (p.get('policy_event_key') != key or post.request_id != expected_id
                or p.get('source_record_sha256') != binding['record_sha256']
                or p.get('commit') != binding['commit']
                or p.get('source_url') != binding['url']
                or not 0 <= (date.fromisoformat(plan['date']) - date.fromisoformat(event['date'])).days <= 4):
            return False
        sources = [event['source'], *bundle['background_sources'],
                   *(r['source'] for r in bundle['company_links']),
                   *(r['source'] for r in bundle['milestones'])]
        checks = p.get('policy_source_checks', [])
        return all(any(all(check.get(k) == source.get(k) for k in
                           ('url', 'published_date', 'excerpt', 'date_excerpt'))
                       and check.get('verified_date') == plan['date']
                       and re.fullmatch(r'[a-f0-9]{64}', check.get('sha256', ''))
                       for check in checks) for source in sources)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def refresh_request(settings, request):
    """Recheck latest revisions/retractions before spending on cached work."""
    if request.provenance.get('investment_mode') != VERSION:
        return request
    from .inputs import collect_requests
    from .research import ResearchRequired
    current, _ = collect_requests(settings)
    match = next((r for r in current if r.id == request.id), None)
    if (match is None or match.data != request.data
            or match.provenance.get('source_record_sha256') != request.provenance.get('source_record_sha256')
            or match.provenance.get('policy_event_key') != request.provenance.get('policy_event_key')):
        raise ResearchRequired('Weekly policy changed, withdrawn, or no longer eligible')
    return verify_evidence(settings, request)


def validate_current_post(settings, post):
    """Ready/save boundaries fail closed without matched, freshly checked input."""
    if post.provenance.get('investment_mode') != VERSION:
        return
    from .inputs import ContentRequest
    from .research import ResearchRequired
    if not post_contract(post, settings.config.get('daily_plan') or {}):
        raise ResearchRequired('Weekly policy post contract mismatch')
    paths = list(settings.artifact_dir.glob(f'{post.as_of_date}-*.json'))
    for path in paths:
        packet = json.loads(path.read_text())
        if packet.get('post', {}).get('request_id') == post.request_id:
            request = ContentRequest(**packet['input'])
            if request.provenance.get('policy_evidence') != post.provenance.get('policy_evidence'):
                break
            refresh_request(settings, request)
            return
    raise ResearchRequired('Weekly policy packet missing or mismatched')


def import_environment(settings, raw):
    """Optional public-only handoff to the existing runner; never a queue entry."""
    if not raw or not enabled(settings):
        return None
    if len(raw.encode('utf-8')) > 48_000:
        raise ValueError('Policy environment metadata exceeds bounded input size')
    bundle = validate_bundle(json.loads(raw),
                             {**settings.config['community'],
                              **settings.config['community'].get('weekly_policy', {})})
    return _register(settings, bundle)
