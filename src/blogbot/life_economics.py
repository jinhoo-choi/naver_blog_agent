"""Owner-selected Wednesday questions, verified against public primary sources."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import date
from urllib.parse import urlsplit

from .core import today_kst
from .weekly_policy import _date_present, _read_official, source_identity

VERSION = 'life-economics-v1'
# Separate from the Friday policy allowlist; never changes KIS admission.
OFFICIAL_HOSTS = {'bok.or.kr', 'www.bok.or.kr', 'fss.or.kr', 'www.fss.or.kr',
                  'boj.or.jp', 'www.boj.or.jp', 'mof.go.jp', 'www.mof.go.jp'}
RULES = [
    ('수요일 life-economics-v1은 사용자의 실제 질문과 읽어 검증한 공식 1차 자료로 작성한다. '
    '환율·금리·소비자금융 등 생활경제를 다루며 KIS 원본·금요일 정책 후보나 종목 연결을 강제하지 않는다.'),
    ('질문에 대한 답 → 검증한 사실과 적용 조건 → 생활에 갖는 의미·확인 방법을 설명한다. '
    '자료의 단위·통화·적용 대상·공표일·통계 기간을 보존하고 사실과 해석을 구분한다.'),
    ('현재 환율·금리·시세를 인용할 때 data_date와 공식 원문의 관측일·조건을 대조한다. '
    'D-1/D0만 현재 수치로 다루고 과거 통계는 해당 기간의 비교·배경으로만 설명한다.'),
    ('검증된 공개 자료만 사용한다. 실제 환전·매매·구매·수익·가족 경험을 창작하지 않는다. '
    '매수·매도 지시·수익 보장·단정적 전망을 쓰지 않고 근거가 없으면 보류한다.'),
    ('공개 본문은 자연스러운 습니다체로 쓰며 내부 기준일 머리말·검수 메모·반복 면책을 넣지 않는다. '
    '판단에 필요한 금융 조건·불확실성·출처는 보존하고 기존 품질·이미지·재시도 예산을 유지한다.'),
]
RULES.append('외부 검색에는 공개용 benchmark_query와 공식 문서명만 쓴다. 개인의 자산·대출 잔액·거래 내역·가족 상황을 검색어로 보내거나 공개하지 않는다.')
GUIDANCE = '\n'.join(RULES)


def _text(value, limit):
    if not isinstance(value, str) or not 3 <= len(value.strip()) <= limit:
        raise ValueError('Missing or oversized life-economics input')
    return value.strip()


def validate_data(data, day=None, *, fresh=True):
    day = day or today_kst()
    if data.get('investment_mode') != VERSION:
        raise ValueError('Life-economics requires an explicit mode')
    sources = data.get('sources')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 3:
        raise ValueError('Life-economics requires one to three official sources')
    result = {'investment_mode': VERSION, 'question': _text(data.get('question'), 1000),
              'benchmark_query': _text(data.get('benchmark_query'), 80), 'sources': []}
    for source in sources:
        url = source_identity(source['url'])
        parts = urlsplit(url)
        if (not (parts.hostname.endswith('.go.kr') or parts.hostname in OFFICIAL_HOSTS)
                or (parts.path in ('', '/') and not parts.query)):
            raise ValueError('Life-economics needs a specific official document')
        published = date.fromisoformat(source['published_date'])
        excerpt = _text(source['excerpt'], 2000)
        date_excerpt = _text(source['date_excerpt'], 300)
        if (fresh and published > day) or not _date_present(str(published), date_excerpt):
            raise ValueError('Life-economics source date is unsupported')
        result['sources'].append({'url': url, 'published_date': str(published),
                                  'excerpt': excerpt, 'date_excerpt': date_excerpt})
    if data.get('data_date') is not None:
        observed = date.fromisoformat(data['data_date'])
        if fresh and not 0 <= (day - observed).days <= 1:
            raise ValueError('Current financial figures require D-1/D0 data')
        if not any(_date_present(str(observed), source['excerpt']) for source in result['sources']):
            raise ValueError('Current data date must occur in a verified source excerpt')
        result['data_date'] = str(observed)
    return result


def request_contract(request, *, fresh=True):
    try:
        return (request.category == 'investment' and request.provenance.get('source') == 'owner_input'
                and request.provenance.get('investment_mode') == VERSION
                and request.provenance.get('life_economics') == validate_data(request.data, fresh=fresh))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def verify_evidence(settings, request):
    from .research import ResearchRequired
    try:
        if not request_contract(request):
            raise ValueError('Life-economics needs a matching owner-input contract')
        bundle = validate_data(request.data)
        checks = []
        for source in bundle['sources']:
            text = _read_official(source['url'])
            if not all(' '.join(source[k].split()) in text for k in ('excerpt', 'date_excerpt')):
                raise ValueError('Official source does not support the supplied excerpt')
            checks.append({**source, 'verified_date': str(today_kst()),
                           'sha256': hashlib.sha256(text.encode()).hexdigest()})
        return replace(request, provenance={**request.provenance, 'life_economics_checks': checks})
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ResearchRequired('Life-economics official-source verification required') from exc


def post_contract(post, plan):
    try:
        p = post.provenance
        if (post.category != 'investment' or p.get('source') != 'owner_input'
                or p.get('investment_mode') != VERSION or plan.get('investment_mode') != VERSION):
            return False
        bundle = validate_data(p['life_economics'], date.fromisoformat(plan['date']))
        if not post.source_urls or not set(post.source_urls) <= {s['url'] for s in bundle['sources']}:
            return False
        body_urls = {u.rstrip('.,') for u in re.findall(r'https?://[^\s<>\]\)]+', post.body)}
        if not body_urls <= {s['url'] for s in bundle['sources']}:
            return False
        checks = p.get('life_economics_checks', [])
        return all(any(all(check.get(k) == source[k] for k in source)
                       and check.get('verified_date') == plan['date']
                       and re.fullmatch(r'[a-f0-9]{64}', check.get('sha256', ''))
                       for check in checks) for source in bundle['sources'])
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def refresh_request(settings, request):
    from .inputs import ContentRequest
    from .research import ResearchRequired
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', request.id):
        raise ResearchRequired('Life-economics input identity is invalid')
    path = settings.inbox_dir / request.id / 'request.json'
    try:
        current = ContentRequest(**json.loads(path.read_text()))
        if (current.id != request.id or current.data != request.data or current.category != request.category
                or not request_contract(current)):
            raise ValueError('Life-economics owner input changed')
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise ResearchRequired('Life-economics input missing or changed') from exc
    return verify_evidence(settings, request)


def validate_current_post(settings, post):
    from .inputs import ContentRequest
    from .research import ResearchRequired
    if not post_contract(post, settings.config.get('daily_plan') or {}):
        raise ResearchRequired('Life-economics post contract mismatch')
    for path in settings.artifact_dir.glob(f'{post.as_of_date}-*.json'):
        packet = json.loads(path.read_text())
        if packet.get('post', {}).get('request_id') == post.request_id:
            request = ContentRequest(**packet['input'])
            if (request.id != post.request_id or request.category != post.category
                    or request.provenance.get('daily_plan') != settings.config.get('daily_plan')
                    or request.provenance.get('life_economics') != post.provenance.get('life_economics')):
                break
            refresh_request(settings, request)
            return
    raise ResearchRequired('Life-economics packet missing or mismatched')
