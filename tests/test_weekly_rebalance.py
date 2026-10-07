"""Offline boundaries for the October 12 weekly rebalance and Wednesday evidence."""
import copy
import hashlib
import io
import json
import socket
import zipfile
from collections import Counter
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from test_daily_plan import set_clock, settings_on

from blogbot import cloud, investment, life_economics, llm, weekly_policy
from blogbot.config import load_settings
from blogbot.core import PostDraft, connect_db, reserve_attempt, save_post, today_kst
from blogbot.images import atomic_json
from blogbot.inputs import ContentRequest, collect_requests, enqueue_file
from blogbot.pipeline import run_daily
from blogbot.planning import (
    active_plan,
    matches_post,
    matches_request,
    ready_categories,
    resolve_plan,
)
from blogbot.research import ResearchRequired, prepare_request

LIFE = 'life-economics-v1'
POLICY = 'weekly-policy-v1'
NEW = 'weekly-2221-v1'
WEDNESDAY = '2026-10-14'
SOURCE_URL = 'https://www.bok.or.kr/portal/bbs/B0000232/view.do?nttId=10100420'
WEEK = ['parenting', 'origins', 'investment', 'exercise', 'investment', 'parenting', 'origins']


@pytest.fixture(autouse=True)
def never_contact_production(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('Offline rebalance test attempted an unmocked network or paid call')
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', blocked)
    monkeypatch.setattr('blogbot.responses.request_json', blocked)
    monkeypatch.setattr('blogbot.llm.request_json', blocked)
    monkeypatch.setattr('blogbot.inputs.fetch_community', blocked)


def life_data(day=WEDNESDAY):
    return {'investment_mode': LIFE,
            'question': '원화와 엔화 환율의 표시 단위는 어떻게 비교하나요?',
            'benchmark_query': '원화 엔화 환율 표시 단위 비교',
            'sources': [{'url': SOURCE_URL, 'published_date': day,
                         'excerpt': '환율은 서로 다른 통화의 교환 비율을 나타냅니다.',
                         'date_excerpt': f'자료 게시일 {day}'}]}


def enqueue_life(settings, *, identity='owner-yen-question', data=None):
    data = copy.deepcopy(data if data is not None else life_data())
    source = settings.db_path.parent / f'{identity}.json'
    source.write_text(json.dumps({'id': identity, 'category': 'investment', **data},
                                 ensure_ascii=False))
    enqueue_file(settings, source)
    path = settings.inbox_dir / identity / 'request.json'
    return ContentRequest(**json.loads(path.read_text()))


def official_html(monkeypatch, data, *, html=None, content_type='text/html; charset=utf-8',
                  error=None):
    """Exercise the real bounded HTML reader; mock only its HTTP transport."""
    calls = []
    sources = data['sources']
    pages = {s['url']: (f'<html><body><p>{s["date_excerpt"]}</p>'
                        f'<p>{s["excerpt"]}</p></body></html>').encode() for s in sources}

    class Response:
        def __init__(self, payload):
            self.headers = {'Content-Type': content_type}
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            assert limit == 2_000_001
            return self.payload[:limit]

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            assert timeout == 25
            if error:
                raise error
            return Response(html.encode() if html is not None else pages[request.full_url])

    monkeypatch.setattr(weekly_policy, 'build_opener', lambda *args: Opener())
    return calls


def verified_packet(settings, monkeypatch, *, identity='owner-yen-question'):
    request = enqueue_life(settings, identity=identity)
    calls = official_html(monkeypatch, request.data)
    request = life_economics.verify_evidence(settings, request)
    plan = active_plan(settings)
    request = replace(request, provenance={**request.provenance, 'daily_plan': plan,
                                           'editorial_type': 'article'})
    post = PostDraft('investment', '투자공부', request.data['question'], '환율의 표시 단위',
                     '환율은 두 통화의 교환 비율입니다.\n\n## 표시 단위 확인\n\n공식 자료를 확인합니다.',
                     ['환율'], [SOURCE_URL], str(today_kst()), 27, 'APPROVED',
                     request_id=request.id, provenance=copy.deepcopy(request.provenance))
    with connect_db(settings.db_path) as conn:
        ident = save_post(conn, post)
    path = settings.artifact_dir / f'{post.as_of_date}-{ident:05d}.json'
    atomic_json(path, {'post': asdict(post), 'input': asdict(request),
                       'review': {'scores': [5, 5, 4, 5, 4, 4], 'total': 27, 'decision': 'PASS'}})
    return request, post, ident, path, calls


@pytest.mark.parametrize('offset', range(7))
def test_october_5_to_11_exact_historical_plans_and_owner_holds(tmp_path, monkeypatch, offset):
    day = date(2026, 10, 5) + timedelta(days=offset)
    settings = settings_on(tmp_path, monkeypatch, str(day), keep_reservation=True)
    category = ['parenting', 'origins', 'parenting', 'exercise', 'investment',
                'parenting', 'parenting'][offset]
    expected = {'version': 'daily-deep-511-v1' if offset == 0 else 'weekly-4111-v1',
                'date': str(day), 'target': 1, 'category': category,
                'editorial_types': ['article', 'ai_tutorial'] if category == 'parenting'
                else ['article'], 'depth': 'short' if category == 'origins' else 'deep'}
    holds = {0: {'category': category, 'kind': 'existing_owner_draft'},
             1: {'category': category, 'kind': 'existing_owner_draft'},
             2: {'category': category, 'kind': 'owner_preparation_pending'},
             4: {'category': category, 'kind': 'owner_preparation_pending',
                 'request_id': 'owner-20261009-new-policy'},
             5: {'category': category, 'kind': 'owner_input_pending'},
             6: {'category': category, 'kind': 'owner_input_pending'}}
    if offset == 4:
        expected['investment_mode'] = POLICY
    if offset in holds:
        expected['reservation'] = holds[offset]
    assert active_plan(settings) == expected
    if offset in holds:
        result = run_daily(settings, count=5)
        assert result == [{'status': 'EDITORIAL_SLOT_RESERVED', 'date': str(day),
                           'category': category, 'reason': holds[offset]['kind'],
                           **({'request_id': holds[offset]['request_id']}
                              if 'request_id' in holds[offset] else {})}]
        assert not settings.db_path.exists()
        assert not settings.inbox_dir.exists()


@pytest.mark.parametrize('offset', range(14))
def test_new_two_week_slots_depth_routes_and_unchanged_budgets(tmp_path, monkeypatch, offset):
    day = date(2026, 10, 12) + timedelta(days=offset)
    settings = settings_on(tmp_path, monkeypatch, str(day), keep_reservation=True)
    category = WEEK[day.weekday()]
    plan = active_plan(settings)
    assert plan == {'version': NEW, 'date': str(day), 'target': 1, 'category': category,
                    'editorial_types': ['article', 'ai_tutorial'] if category == 'parenting'
                    else ['article'], 'depth': 'short' if category == 'origins' else 'deep',
                    **({'investment_mode': LIFE if day.weekday() == 2 else POLICY}
                       if category == 'investment' else {})}
    assert settings.daily_count == settings.config['blog']['daily_max'] == 1
    assert sum(c['max_daily'] for c in settings.config['categories'].values()) == 1
    assert settings.config['categories'][category]['max_daily'] == 1
    assert settings.config['community']['enabled'] == (day.weekday() == 4)
    assert settings.config['blog']['review_pass_score'] == 24
    assert settings.config['images']['max_attempts'] == 2
    assert [settings.config['images'][c + '_count'] for c in
            ['parenting', 'exercise', 'investment']] == [5, 5, 3]
    if category == 'origins':
        assert day.weekday() in {1, 6}
        assert not any('실제 캡처/직접 제공 사진' in rule
                       for rule in settings.config['categories']['origins']['rules'])


def test_two_week_2221_totals_and_measurement_metadata_never_expire_plan(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-12')
    start = date(2026, 10, 12)
    assert Counter(resolve_plan(settings.config, start + timedelta(days=i))['category']
                   for i in range(14)) == {'parenting': 4, 'investment': 4, 'origins': 4, 'exercise': 2}
    policy = settings.config['operating_plan']
    assert {key: policy[key] for key in ('measurement_start', 'measurement_end',
                                        'review_not_before', 'measurement_post_hours')} == {
        'measurement_start': '2026-10-12', 'measurement_end': '2026-10-25',
        'review_not_before': '2026-10-28', 'measurement_post_hours': 72}
    for text in ['2026-10-26', '2026-10-28', '2026-11-01', '2027-01-06']:
        day = date.fromisoformat(text)
        plan = resolve_plan(settings.config, day)
        assert plan['version'] == NEW and plan['target'] == 1
        assert plan['category'] == WEEK[day.weekday()]


@pytest.mark.parametrize('stamp,day,version,category', [
    ('2026-10-11T14:59:59+00:00', '2026-10-11', 'weekly-4111-v1', 'parenting'),
    ('2026-10-11T15:00:00+00:00', '2026-10-12', NEW, 'parenting'),
    ('2026-10-13T14:59:59+00:00', '2026-10-13', NEW, 'origins'),
    ('2026-10-13T15:00:00+00:00', WEDNESDAY, NEW, 'investment'),
])
def test_rebalance_uses_kst_midnight_boundary(tmp_path, monkeypatch, stamp, day, version, category):
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    set_clock(monkeypatch, datetime.fromisoformat(stamp))
    plan = active_plan(load_settings())
    assert (plan['date'], plan['version'], plan['category']) == (day, version, category)


def test_loaded_previous_week_plan_must_reload_at_effective_kst_boundary(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-11', keep_reservation=True)
    set_clock(monkeypatch, datetime.fromisoformat('2026-10-11T15:00:00+00:00'))
    with pytest.raises(ValueError, match='Reload'):
        run_daily(settings)


@pytest.mark.parametrize('invalid', [False, True])
def test_missing_or_invalid_wednesday_owner_input_never_reads_kis_or_buys_work(
        tmp_path, monkeypatch, invalid):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    if invalid:
        request = enqueue_life(settings)
        path = settings.inbox_dir / request.id / 'request.json'
        raw = json.loads(path.read_text())
        raw['data']['sources'][0]['url'] = 'https://unofficial.example/yen'
        atomic_json(path, raw)
    result = run_daily(settings, count=5)
    assert result == ([{'status': 'INPUT_REJECTED'}] if invalid else [
        {'status': 'PLANNED_INPUT_REQUIRED', 'category': 'investment', 'editorial_types': ['article']}])
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 0


@pytest.mark.parametrize('url', [
    'http://www.bok.or.kr/document', 'https://unofficial.example/document',
    'https://www.bok.or.kr.evil.example/document', 'https://evil.go.kr.example/document',
    'https://go.kr/document', 'https://user:password@www.bok.or.kr/document',
    'https://www.bok.or.kr:444/document', 'https://www.bok.or.kr/',
    'https://www.mof.go.jp/', 'file:///tmp/evidence.html',
])
def test_owner_source_requires_https_specific_official_document(tmp_path, monkeypatch, url):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    data['sources'][0]['url'] = url
    with pytest.raises(ValueError):
        enqueue_life(settings, data=data)
    assert not list(settings.inbox_dir.glob('*/request.json'))


@pytest.mark.parametrize('url', [
    'https://www.mohw.go.kr/board?id=7', SOURCE_URL,
    'https://www.fss.or.kr/fss/bbs/B0000188/view.do?nttId=7',
    'https://www.boj.or.jp/statistics/outline/exp/exrate.htm',
    'https://www.mof.go.jp/policy/international_policy/reference/feio/index.htm',
])
def test_official_source_allowlist_accepts_exact_document_hosts(tmp_path, monkeypatch, url):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    data['sources'][0]['url'] = url
    request = enqueue_life(settings, data=data)
    assert request.provenance['life_economics']['sources'][0]['url'] == url


@pytest.mark.parametrize('field,value', [
    ('question', ''), ('question', ' '), ('question', 'x' * 1001),
    ('investment_mode', POLICY), ('investment_mode', ''),
    ('sources', []), ('sources', None),
    ('benchmark_query', ''), ('benchmark_query', None), ('benchmark_query', 'x' * 201),
])
def test_owner_contract_rejects_missing_question_mode_or_sources(tmp_path, monkeypatch, field, value):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    data[field] = value
    with pytest.raises((ValueError, TypeError, KeyError)):
        enqueue_life(settings, data=data)


@pytest.mark.parametrize('field,value', [
    ('excerpt', ''), ('excerpt', ' '), ('excerpt', None), ('excerpt', 'x' * 2001),
    ('date_excerpt', ''), ('date_excerpt', '자료 게시일 2026-10-13'),
    ('published_date', '2026-10-15'), ('published_date', 'not-a-date'),
])
def test_source_needs_nonempty_excerpts_matching_nonfuture_publication(
        tmp_path, monkeypatch, field, value):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    data['sources'][0][field] = value
    with pytest.raises((ValueError, TypeError, KeyError)):
        enqueue_life(settings, data=data)


@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_owner_contract_accepts_one_to_three_sources_only(tmp_path, monkeypatch, count):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    data['sources'] = [{**data['sources'][0], 'url': f'https://www.bok.or.kr/document?id={i}'}
                       for i in range(count)]
    if count > 3:
        with pytest.raises(ValueError):
            enqueue_life(settings, data=data)
    else:
        assert len(enqueue_life(settings, data=data).data['sources']) == count


@pytest.mark.parametrize('observed,accepted', [
    (None, True), ('2026-10-13', True), ('2026-10-14', True),
    ('2026-10-12', False), ('2026-10-15', False), ('not-a-date', False),
])
def test_current_data_preserves_d_minus_one_or_today_rule(tmp_path, monkeypatch, observed, accepted):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    if observed is not None:
        data['data_date'] = observed
        data['sources'][0]['excerpt'] += f' 관측일: {observed}. 공식 자료입니다.'
    if accepted:
        assert enqueue_life(settings, data=data).data.get('data_date') == observed
    else:
        with pytest.raises(ValueError):
            enqueue_life(settings, data=data)


@pytest.mark.parametrize('problem', ['network', 'missing_claim', 'missing_date', 'hidden_claim', 'pdf'])
def test_real_source_verification_holds_before_any_paid_preparation(tmp_path, monkeypatch, problem):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    request = enqueue_life(settings)
    source = request.data['sources'][0]
    kwargs = {
        'network': {'error': OSError('offline source unavailable')},
        'missing_claim': {'html': f'<p>{source["date_excerpt"]}</p><p>다른 내용입니다.</p>'},
        'missing_date': {'html': f'<p>{source["excerpt"]}</p>'},
        'hidden_claim': {'html': f'<p>{source["date_excerpt"]}</p><script>{source["excerpt"]}</script>'},
        'pdf': {'content_type': 'application/pdf'},
    }[problem]
    calls = official_html(monkeypatch, request.data, **kwargs)
    results = run_daily(settings, count=5)
    assert [result['status'] for result in results] == ['RESEARCH_REQUIRED']
    assert calls == [SOURCE_URL]
    with connect_db(settings.db_path) as conn:
        assert [row[0] for row in conn.execute('SELECT status FROM attempts')] == ['RESEARCH_REQUIRED']
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 0


def test_valid_wednesday_primary_evidence_records_verified_provenance(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    settings.config['editorial']['enabled'] = False
    request = enqueue_life(settings)
    calls = official_html(monkeypatch, request.data)
    checked = prepare_request(settings, request)
    source = request.data['sources'][0]
    rendered = f'{source["date_excerpt"]} {source["excerpt"]}'
    assert calls == [SOURCE_URL]
    assert checked.provenance['life_economics_checks'] == [{
        **source, 'verified_date': WEDNESDAY,
        'sha256': hashlib.sha256(rendered.encode()).hexdigest()}]
    assert checked.provenance['source'] == 'owner_input'
    assert investment.request_contract(checked, active_plan(settings))
    assert 'policy_event_key' not in checked.provenance


def test_wednesday_and_friday_request_modes_cannot_cross_slots(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    owner = enqueue_life(settings)
    friday = ContentRequest('community-policy', 'investment', {'kind': 'policy'},
                            provenance={'investment_mode': POLICY, 'policy_event_key': 'a' * 64})
    wednesday_plan = resolve_plan(settings.config, date(2026, 10, 14))
    friday_plan = resolve_plan(settings.config, date(2026, 10, 16))
    assert matches_request(owner, wednesday_plan)
    assert matches_request(friday, friday_plan)
    assert not matches_request(owner, friday_plan)
    assert not matches_request(friday, wednesday_plan)
    legacy = ContentRequest('legacy', 'investment', {'kind': 'research'})
    assert not matches_request(legacy, wednesday_plan)
    assert not matches_request(legacy, friday_plan)


def test_friday_collection_does_not_use_queued_wednesday_question_as_fallback(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-16')
    enqueue_life(settings)
    monkeypatch.setattr('blogbot.inputs.fetch_community', lambda _: ([], {
        'snapshot_at': '2026-10-16T07:00:00+09:00', 'snapshot_date': '2026-10-16',
        'repository': settings.config['community']['repository'], 'commit': 'a' * 40}))
    monkeypatch.setattr('blogbot.inputs.fetch_policy_history', lambda *args: [])
    requests, notices = collect_requests(settings)
    assert requests == []
    assert notices[0]['status'] == 'NO_ELIGIBLE_INVESTMENT'


@pytest.mark.parametrize('field,value', [
    ('investment_mode', POLICY), ('investment_mode', 'invented-route'),
    ('source', 'community'), ('life_economics_checks', []),
])
def test_ready_contract_rejects_wrong_mode_owner_or_missing_verification(
        tmp_path, monkeypatch, field, value):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    _, post, ident, _, _ = verified_packet(settings, monkeypatch)
    post.provenance[field] = value
    assert not matches_post(post, active_plan(settings))
    with connect_db(settings.db_path) as conn:
        conn.execute('UPDATE posts SET provenance_json=? WHERE id=?',
                     (json.dumps(post.provenance), ident))
        conn.commit()
        assert ready_categories(settings, conn) == set()


def test_verified_wednesday_attempt_budget_still_allows_only_one_slot(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    first = enqueue_life(settings, identity='first-owner-question')
    second = enqueue_life(settings, identity='second-owner-question')
    with connect_db(settings.db_path) as conn:
        attempt = reserve_attempt(conn, settings.config, 5, [first, second])
        assert attempt is not None
        assert reserve_attempt(conn, settings.config, 5, [first, second]) is None
        assert json.loads(conn.execute('SELECT plan_json FROM attempts').fetchone()[0]) == active_plan(settings)


def test_valid_pack_unpack_preserves_checked_owner_manifest_and_one_handoff(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    request, post, _, _, calls = verified_packet(settings, monkeypatch)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    bundle = tmp_path / 'bundle.enc'
    cloud.pack(settings, bundle)
    assert len(json.loads((tmp_path / 'ready.json').read_text())['posts']) == 1
    assert calls == [SOURCE_URL, SOURCE_URL]
    destination = tmp_path / 'restored'
    cloud.extract_bundle(bundle.read_bytes(), destination, key)
    restored = replace(settings, db_path=destination / 'blog.db', artifact_dir=destination / 'drafts',
                       inbox_dir=destination / 'inbox')
    cloud.filter_ready(destination, {'verified_date': WEDNESDAY, 'records': []},
                       active_plan(restored), restored)
    ready = json.loads((destination / 'ready.json').read_text())
    assert ready['daily_plan'] == active_plan(settings)
    assert [item['post']['request_id'] for item in ready['posts']] == [request.id]
    assert ready['posts'][0]['post']['provenance'] == post.provenance
    assert calls == [SOURCE_URL] * 3
    assert json.loads((restored.inbox_dir / request.id / 'request.json').read_text())['data'] == request.data


@pytest.mark.parametrize('tamper', ['queue_excerpt', 'packet_mode', 'post_source', 'post_mode'])
def test_pack_withholds_tampered_wednesday_artifacts_without_buying_work(tmp_path, monkeypatch, tamper):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    request, post, ident, packet_path, _ = verified_packet(settings, monkeypatch)
    if tamper == 'queue_excerpt':
        path = settings.inbox_dir / request.id / 'request.json'
        data = json.loads(path.read_text())
        data['data']['sources'][0]['excerpt'] = '공식 자료에 없는 임의 주장입니다.'
        atomic_json(path, data)
    elif tamper == 'packet_mode':
        data = json.loads(packet_path.read_text())
        data['input']['data']['investment_mode'] = POLICY
        data['input']['provenance']['investment_mode'] = POLICY
        atomic_json(packet_path, data)
    else:
        if tamper == 'post_source':
            post.provenance['life_economics']['sources'][0]['url'] = 'https://unofficial.example/fake'
        else:
            post.provenance['investment_mode'] = POLICY
        with connect_db(settings.db_path) as conn:
            conn.execute('UPDATE posts SET provenance_json=? WHERE id=?',
                         (json.dumps(post.provenance), ident))
            conn.commit()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', Fernet.generate_key().decode())
    cloud.pack(settings, tmp_path / 'bundle.enc')
    assert json.loads((tmp_path / 'ready.json').read_text())['posts'] == []
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == 'APPROVED'


@pytest.mark.parametrize('tamper', ['ready_source', 'ready_mode', 'queue_excerpt', 'packet_mode'])
def test_unpack_rechecks_tampered_source_and_mode_before_releasing_handoff(tmp_path, monkeypatch, tamper):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    request, _, _, packet_path, _ = verified_packet(settings, monkeypatch)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    bundle = tmp_path / 'bundle.enc'
    cloud.pack(settings, bundle)
    # Model a stale/altered archive from another authenticated run, then apply the
    # same extract-plus-filter sequence used by the public unpack command.
    with zipfile.ZipFile(io.BytesIO(Fernet(key.encode()).decrypt(bundle.read_bytes()))) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    if tamper.startswith('ready_'):
        name = 'ready.json'
        data = json.loads(members[name])
        p = data['posts'][0]['post']['provenance']
        if tamper == 'ready_source':
            p['life_economics']['sources'][0]['url'] = 'https://unofficial.example/fake'
        else:
            p['investment_mode'] = POLICY
    elif tamper == 'queue_excerpt':
        name = f'inbox/{request.id}/request.json'
        data = json.loads(members[name])
        data['data']['sources'][0]['excerpt'] = '원문에 없는 임의 주장입니다.'
    else:
        name = 'drafts/' + packet_path.name
        data = json.loads(members[name])
        data['input']['data']['investment_mode'] = POLICY
        data['input']['provenance']['investment_mode'] = POLICY
    members[name] = json.dumps(data, ensure_ascii=False).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
    destination = tmp_path / 'restored'
    cloud.extract_bundle(Fernet(key.encode()).encrypt(buffer.getvalue()), destination, key)
    restored = replace(settings, db_path=destination / 'blog.db', artifact_dir=destination / 'drafts',
                       inbox_dir=destination / 'inbox')
    cloud.filter_ready(destination, {'verified_date': WEDNESDAY, 'records': []},
                       active_plan(restored), restored)
    assert json.loads((destination / 'ready.json').read_text())['posts'] == []


def model_fixture(settings, monkeypatch):
    request = enqueue_life(settings)
    official_html(monkeypatch, request.data)
    request = life_economics.verify_evidence(settings, request)
    calls = []
    payload = {'title': '환율 표시 단위', 'subcategory': '투자공부', 'body': '환율을 설명합니다.',
               'tags': [], 'source_urls': [SOURCE_URL], 'scores': [4] * 6,
               'total': 24, 'decision': 'PASS', 'issues': [], 'blocking_issues': [],
               'rewrite_instructions': '', 'source_checks': [
                   {'claim': '환율 표시 단위', 'source_url': SOURCE_URL,
                    'evidence': request.data['sources'][0]['excerpt'], 'status': 'SUPPORTED'}]}
    response = {'output': [{'status': 'completed', 'action': {'type': 'open_page', 'url': SOURCE_URL}}]}

    def mock_json(*args, **kwargs):
        calls.append(kwargs)
        return copy.deepcopy(payload), copy.deepcopy(response)

    monkeypatch.setattr(llm, 'request_json', mock_json)
    client = llm.BlogLLM.__new__(llm.BlogLLM)
    client.client = client.journal = None
    client.model = 'offline-writer'
    client.review_model = 'offline-reviewer'
    client.writer_prompt = (settings.root / 'prompts/writer.md').read_text()
    client.reviewer_prompt = (settings.root / 'prompts/reviewer.md').read_text()
    return client, request, payload, response, calls


def test_actual_four_model_paths_route_wednesday_without_changing_call_budgets(tmp_path, monkeypatch):
    from blogbot.pre_review import DraftCandidate
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    client, request, payload, _, calls = model_fixture(settings, monkeypatch)
    info = settings.config['categories']['investment']
    post = client.create_draft(request, info, [])
    review = client.review(post, info, request)
    client.rewrite(post, info, review, request)
    corrected = client.correct_draft(DraftCandidate(payload, [SOURCE_URL]), info,
                                     ['invalid_field_tags'], request)
    assert review['decision'] == 'PASS'
    assert post.category == 'investment' and post.provenance == request.provenance
    assert corrected.observed == [SOURCE_URL]
    assert [call['stage'] for call in calls] == ['writer', 'reviewer', 'rewrite', 'pre_review_correction']
    assert [call['model'] for call in calls] == [
        'offline-writer', 'offline-reviewer', 'offline-writer', 'offline-writer']
    for call in calls:
        assert life_economics.GUIDANCE in call['input']
        assert request.data['question'] in call['input']
        assert 'kis-community-bot의 심사 통과 원본 facts/src/body를 기반으로만 작성' not in call['input']
    assert all(call['max_tool_calls'] == 3 for call in calls[:3])
    assert 'tools' not in calls[3] and calls[3]['max_output_tokens'] == 12000


def test_current_data_date_cannot_be_relabelled_without_matching_official_excerpt(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    data['data_date'] = WEDNESDAY
    data['sources'][0]['excerpt'] += ' 과거 관측일: 2026-10-01.'
    with pytest.raises(ValueError, match='date.*excerpt'):
        enqueue_life(settings, data=data)


def test_current_observation_date_must_be_read_from_real_page_before_paid_work(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    data['data_date'] = WEDNESDAY
    data['sources'][0]['excerpt'] += f' 관측일: {WEDNESDAY}.'
    request = enqueue_life(settings, data=data)
    source = request.data['sources'][0]
    official_html(monkeypatch, request.data,
                  html=f'<p>{source["date_excerpt"]}</p><p>과거 관측일: 2026-10-01.</p>')
    with pytest.raises(ResearchRequired):
        prepare_request(settings, request)


def test_historic_background_publication_is_not_itself_a_current_price_claim(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data('2020-04-01')
    request = enqueue_life(settings, data=data)
    official_html(monkeypatch, request.data)
    checked = life_economics.verify_evidence(settings, request)
    assert checked.provenance['life_economics_checks'][0]['published_date'] == '2020-04-01'
    assert 'data_date' not in checked.data


def test_expired_wednesday_figures_do_not_poison_friday_queue_scan(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    data['data_date'] = WEDNESDAY
    data['sources'][0]['excerpt'] += f' 관측일: {WEDNESDAY}.'
    enqueue_life(settings, data=data)
    set_clock(monkeypatch, datetime.fromisoformat('2026-10-16T03:00:00+00:00'))
    friday = load_settings()
    monkeypatch.setattr('blogbot.inputs.fetch_community', lambda _: ([], {
        'snapshot_at': '2026-10-16T07:00:00+09:00', 'snapshot_date': '2026-10-16',
        'repository': friday.config['community']['repository'], 'commit': 'a' * 40}))
    monkeypatch.setattr('blogbot.inputs.fetch_policy_history', lambda *args: [])
    requests, notices = collect_requests(friday)
    assert requests == []
    assert [notice['status'] for notice in notices] == ['NO_ELIGIBLE_INVESTMENT']


def test_public_benchmark_query_never_falls_back_to_private_owner_question(tmp_path, monkeypatch):
    from blogbot.research import public_query
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    data = life_data()
    data['question'] = '비공개 개인 메모: 가족 계좌 상황을 포함한 실제 질문입니다.'
    request = enqueue_life(settings, data=data)
    assert public_query(request) == data['benchmark_query']
    assert data['question'] not in public_query(request)
    request.data.pop('benchmark_query')
    with pytest.raises(ResearchRequired, match='public benchmark_query'):
        public_query(request)


@pytest.mark.parametrize('tamper', ['mode', 'bundle', 'source'])
def test_conflicting_persisted_owner_provenance_is_rejected_before_paid_work(tmp_path, monkeypatch, tamper):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    request = enqueue_life(settings)
    path = settings.inbox_dir / request.id / 'request.json'
    raw = json.loads(path.read_text())
    if tamper == 'mode':
        raw['provenance']['investment_mode'] = POLICY
    elif tamper == 'bundle':
        raw['provenance']['life_economics']['question'] = '소유자가 승인하지 않은 다른 질문'
    else:
        raw['provenance']['source'] = 'community'
    atomic_json(path, raw)
    assert run_daily(settings) == [{'status': 'INPUT_REJECTED'}]
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


@pytest.mark.parametrize('mode', [None, POLICY, 'invented-route'])
@pytest.mark.parametrize('entry', ['verify_evidence', 'refresh_request'])
def test_expected_wednesday_mode_cannot_bypass_routing_checks(tmp_path, monkeypatch, mode, entry):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    request = enqueue_life(settings)
    if mode is None:
        request.provenance.pop('investment_mode')
    else:
        request.provenance['investment_mode'] = mode
    with pytest.raises((ResearchRequired, ValueError)):
        getattr(investment, entry)(settings, request)


@pytest.mark.parametrize('tamper', ['source_urls', 'body_url', 'check_excerpt', 'check_day', 'check_hash'])
def test_post_provenance_cannot_authorize_unregistered_or_unchecked_evidence(tmp_path, monkeypatch, tamper):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    _, post, _, _, _ = verified_packet(settings, monkeypatch)
    if tamper == 'source_urls':
        post.source_urls = ['https://www.bok.or.kr/different-document']
    elif tamper == 'body_url':
        post.body += '\n출처: https://unofficial.example/article'
    elif tamper == 'check_excerpt':
        post.provenance['life_economics_checks'][0]['excerpt'] = '검증되지 않은 다른 문장'
    elif tamper == 'check_day':
        post.provenance['life_economics_checks'][0]['verified_date'] = '2026-10-13'
    else:
        post.provenance['life_economics_checks'][0]['sha256'] = 'invalid-digest'
    assert not matches_post(post, active_plan(settings))


@pytest.mark.parametrize('stage', ['writer', 'rewrite', 'reviewer'])
def test_model_search_cannot_expand_registered_wednesday_source_scope(tmp_path, monkeypatch, stage):
    settings = settings_on(tmp_path, monkeypatch, WEDNESDAY)
    client, request, payload, response, _ = model_fixture(settings, monkeypatch)
    info = settings.config['categories']['investment']
    post = client.create_draft(request, info, [])
    unregistered = 'https://www.bok.or.kr/unregistered-document'
    response['output'].append({'status': 'completed', 'action': {
        'type': 'open_page', 'url': unregistered}})
    if stage == 'reviewer':
        payload['source_checks'][0]['source_url'] = unregistered
        review = client.review(post, info, request)
        assert review['decision'] == 'REWRITE'
        assert review['blocking_issues']
    else:
        payload['source_urls'] = [unregistered]
        with pytest.raises(ValueError, match='Source URL'):
            if stage == 'writer':
                client.create_draft(request, info, [])
            else:
                client.rewrite(post, info, {'decision': 'REWRITE'}, request)
