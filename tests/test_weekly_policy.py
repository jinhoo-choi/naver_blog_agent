"""Offline weekly-policy admission, identity, evidence and replay boundaries."""
import copy
import json
from dataclasses import replace

import pytest
from test_daily_plan import plan_draft, settings_on

from blogbot import inputs
from blogbot.core import connect_db, reserve_attempt
from blogbot.inputs import collect_requests, community_request
from blogbot.planning import matches_post
from blogbot.research import ResearchRequired, prepare_primary_evidence
from blogbot.weekly_policy import (
    VERSION,
    bundle_path,
    evidence_for,
    import_evidence,
    record_digest,
    refresh_request,
)


def source(day='2026-10-09', url='https://www.mohw.go.kr/board?id=1', text='새 돌봄 정책 발표'):
    return {'url': url, 'published_date': day, 'excerpt': text,
            'date_excerpt': f'등록일 {day}'}


def fixture(settings, day='2026-10-09', identity='policy-1', sha='a' * 40):
    record = {'id': identity, 'kind': 'policy', 'facts': f'보도일: {day}\n새 돌봄 정책 발표',
              'src': 'https://www.mohw.go.kr/board?id=1', 'body': '원본 사실 요약',
              'stock_name': '게시판종목', 'stock_code': '123456',
              'theme_assigned': True, 'board_mapping': 'SECTOR_PROXY',
              'score': {'factual': 5, 'useful': 4, 'natural': 4, 'compliant': 5,
                        'gain': 4, 'fit': 4, 'fatal': []}}
    origin = {'repository': settings.config['community']['repository'], 'commit': sha,
              'snapshot_date': '2026-10-09', 'snapshot_at': '2026-10-09T07:00:00+09:00'}
    official = source(day)
    bundle = {'version': VERSION, 'source': {'repository': origin['repository'], 'commit': sha,
               'record_id': identity, 'url': record['src'], 'record_sha256': record_digest(record)},
              'event': {'date': day, 'novelty': 'new_announcement', 'stage': 'announced',
                        'change': '새 돌봄 정책 발표', 'export_excerpt': '새 돌봄 정책 발표',
                        'source': official}, 'background_sources': [],
              'company_links': [{'company': 'NHN', 'relationship': 'previous_project',
                                 'claim': '기존 사업 수행 기업',
                                 'source': source('2026-05-07', 'https://inside.nhn.com/news/936',
                                                  'NHN 기존 사업 수행')}],
              'milestones': [{'date': None, 'condition': '후속 예산 확정 여부', 'source': official}]}
    return record, origin, bundle


def register(settings, record, origin, bundle):
    bundle['source']['record_sha256'] = record_digest(record)
    path = bundle_path(settings, origin['commit'], record['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, ensure_ascii=False))


def install_fetch(monkeypatch, record, origin, older=()):
    monkeypatch.setattr(inputs, 'fetch_community', lambda _: ([record], origin))
    monkeypatch.setattr(inputs, 'fetch_policy_history', lambda *args: [(record, origin), *older])


def mock_official(monkeypatch, bundle):
    sources = [bundle['event']['source'], *bundle['background_sources'],
               *(r['source'] for r in bundle['company_links']),
               *(r['source'] for r in bundle['milestones'])]
    pages = {}
    for s in sources:
        pages[s['url']] = pages.get(s['url'], '') + ' ' + s['excerpt'] + ' ' + s['date_excerpt']
    monkeypatch.setattr('blogbot.weekly_policy._read_official', lambda url: pages[url])


@pytest.mark.parametrize('day,accepted', [('2026-10-05', True), ('2026-10-04', False),
                                         ('2026-10-09', True), ('2026-10-10', False)])
def test_five_calendar_days_not_export_age(tmp_path, monkeypatch, day, accepted):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings, day)
    register(settings, record, origin, bundle)
    install_fetch(monkeypatch, record, origin)
    requests, notices = collect_requests(settings)
    assert bool(requests) == accepted
    if accepted:
        assert requests[0].data['source_date'] == day
        assert requests[0].data['stock_name'] == requests[0].data['stock_code'] == ''
        assert requests[0].data['policy_evidence']['company_links'][0]['company'] == 'NHN'
        assert requests[0].provenance['source_score'] == record['score']
    else:
        assert notices[0]['status'] == 'NO_ELIGIBLE_INVESTMENT'


@pytest.mark.parametrize('snapshot', ['2026-10-08T08:00:00+09:00', '2026-10-09T06:59:59+09:00',
                                      '2026-10-09T13:00:00+09:00'])
def test_stale_early_or_future_latest_export_holds_before_history(tmp_path, monkeypatch, snapshot):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, _ = fixture(settings)
    origin.update(snapshot_at=snapshot, snapshot_date=snapshot[:10])
    monkeypatch.setattr(inputs, 'fetch_community', lambda _: ([record], origin))
    monkeypatch.setattr(inputs, 'fetch_policy_history', lambda *a: pytest.fail('Read before readiness'))
    requests, notices = collect_requests(settings)
    assert not requests and notices[0]['status'] in {'COMMUNITY_SOURCE_PENDING', 'COMMUNITY_SOURCE_UNAVAILABLE'}


@pytest.mark.parametrize('field,value', [('fatal', ['unsupported']), ('factual', 3),
                                        ('compliant', 3), ('fit', 2)])
def test_evidence_does_not_override_quality(tmp_path, monkeypatch, field, value):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    record['score'][field] = value
    register(settings, record, origin, bundle)
    install_fetch(monkeypatch, record, origin)
    assert not collect_requests(settings)[0]


@pytest.mark.parametrize('kind', ['research', 'disclosure'])
def test_no_research_or_disclosure_fallback(tmp_path, monkeypatch, kind):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    record['kind'] = kind
    register(settings, record, origin, bundle)
    install_fetch(monkeypatch, record, origin)
    assert not collect_requests(settings)[0]


def test_verified_new_step_allows_explicit_old_background_not_market(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    background = source('2026-05-07', text='기존 사업 발표')
    background['url'] += '&old=1'
    background['export_excerpt'] = '보도일: 2026-05-07 기존 사업 발표'
    bundle['event']['novelty'] = 'new_step'
    bundle['background_sources'] = [background]
    record['facts'] += '\n' + background['export_excerpt']
    register(settings, record, origin, bundle)
    install_fetch(monkeypatch, record, origin)
    assert collect_requests(settings)[0]
    record['facts'] += '\n2026-05-07 종가 1000원'
    record['market_as_of_date'] = '2026-10-09'
    register(settings, record, origin, bundle)
    assert not collect_requests(settings)[0]
    record['facts'] = '보도일: 2026-05-07\n새 돌봄 정책 발표'
    register(settings, record, origin, bundle)
    assert not collect_requests(settings)[0]  # Background cannot launder the old primary report.


@pytest.mark.parametrize('mutation', ['id', 'sha', 'digest', 'url', 'tier', 'future', 'milestone'])
def test_bundle_binding_and_semantics_fail_closed(tmp_path, monkeypatch, mutation):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    if mutation == 'id': bundle['source']['record_id'] = 'wrong-id'
    if mutation == 'sha': bundle['source']['commit'] = 'b' * 40
    if mutation == 'digest': bundle['source']['record_sha256'] = '0' * 64
    if mutation == 'url': bundle['source']['url'] = 'https://www.mohw.go.kr/other'
    if mutation == 'tier': bundle['company_links'][0]['relationship'] = 'guaranteed_beneficiary'
    if mutation == 'future': bundle['event']['date'] = '2027-01-01'
    if mutation == 'milestone': bundle['milestones'][0]['date'] = '2027-01-01'
    path = bundle_path(settings, origin['commit'], record['id'])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(bundle))
    with pytest.raises((ValueError, KeyError)):
        evidence_for(settings, record, origin)


def test_source_is_reopened_and_unverified_bundle_does_not_approve(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    register(settings, record, origin, bundle)
    install_fetch(monkeypatch, record, origin)
    request = collect_requests(settings)[0][0]
    post = replace(plan_draft(settings, request.id), provenance={**request.provenance,
                   'daily_plan': settings.config['daily_plan']})
    assert not matches_post(post, settings.config['daily_plan'])
    mock_official(monkeypatch, bundle)
    checked = prepare_primary_evidence(settings, request)
    assert checked.provenance['policy_source_checks']
    assert matches_post(replace(post, provenance={**checked.provenance,
                        'daily_plan': settings.config['daily_plan']}), settings.config['daily_plan'])
    monkeypatch.setattr('blogbot.weekly_policy._read_official', lambda _: 'withdrawn document')
    with pytest.raises(ResearchRequired):
        refresh_request(settings, checked)


def test_newer_failed_retracted_or_changed_record_suppresses_history(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    old, origin, _ = fixture(settings)
    newer = copy.deepcopy(old)
    newer['score']['fatal'] = ['rejected']
    latest = {**origin, 'commit': 'b' * 40}
    commits = [{'sha': 'b' * 40, 'commit': {'committer': {'date': '2026-10-08T22:00:00Z'}}},
               {'sha': 'a' * 40, 'commit': {'committer': {'date': '2026-10-07T22:00:00Z'}}}]
    monkeypatch.setattr(inputs, '_get_json', lambda _: commits)
    monkeypatch.setattr(inputs, '_community_snapshot', lambda *a: ([old], origin))
    for changes in [{'score': newer['score']}, {'retracted': True}, {'kind': 'research'},
                    {'id': 'new-id', 'status': 'withdrawn'}]:
        record = {**old, **changes}
        found = inputs.fetch_policy_history(settings.config['community'], [record], latest)
        assert not any(r['score']['fatal'] == [] and r.get('kind') == 'policy'
                       and not r.get('retracted') for r, _ in found)


def test_bounded_history_pins_blobs_and_fails_incomplete(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, _ = fixture(settings)
    calls = []
    commits = [{'sha': 'a' * 40, 'commit': {'committer': {'date': '2026-10-08T22:00:00Z'}}}]
    def get(url):
        calls.append(url)
        return commits
    monkeypatch.setattr(inputs, '_get_json', get)
    found = inputs.fetch_policy_history(settings.config['community'], [record], origin)
    assert found[0][1]['latest_snapshot'] == origin
    assert 'sha=' + 'a' * 40 in calls[0] and 'since=2026-10-05T00%3A00%3A00%2B09%3A00' in calls[0]
    commits *= 21
    with pytest.raises(ValueError, match='incomplete'):
        inputs.fetch_policy_history(settings.config['community'], [record], origin)


def test_new_upstream_id_same_event_cannot_buy_another_attempt(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    request = community_request(record, origin, settings.config['community'], policy_evidence=bundle)
    with connect_db(settings.db_path) as conn:
        first = reserve_attempt(conn, settings.config, 1, [request])
        assert first
        conn.execute("UPDATE attempts SET day='2026-10-02'")
        conn.commit()
        changed = replace(request, id='community-new-upstream-id')
        assert reserve_attempt(conn, settings.config, 1, [changed]) is None
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1


def test_registration_is_private_evidence_only_and_conflicts_block(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    _, origin, bundle = fixture(settings)
    raw = tmp_path / 'bundle.json'
    raw.write_text(json.dumps(bundle))
    result = import_evidence(settings, raw)
    assert result['candidate_created'] is False and not settings.inbox_dir.exists()
    assert bundle_path(settings, origin['commit'], bundle['source']['record_id']).is_file()
    bundle['event']['change'] = 'different change'
    raw.write_text(json.dumps(bundle))
    with pytest.raises(ValueError, match='Conflicting'):
        import_evidence(settings, raw)


def test_october_ninth_hold_cannot_adopt_alteogen_or_create_candidate(tmp_path, monkeypatch):
    from blogbot.pipeline import run_daily
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09', keep_reservation=True)
    monkeypatch.setattr(inputs, 'fetch_community', lambda _: pytest.fail('Reserved slot'))
    assert run_daily(settings) == [{'status': 'EDITORIAL_SLOT_RESERVED', 'date': '2026-10-09',
        'category': 'investment', 'reason': 'owner_preparation_pending',
        'request_id': 'owner-20261009-new-policy'}]
    old = plan_draft(settings, 'owner-alteogen')
    assert not matches_post(old, settings.config['daily_plan'])


def test_same_day_revalidation_preserves_paid_prompt_identity(tmp_path, monkeypatch):
    from datetime import datetime

    from test_daily_plan import set_clock
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    register(settings, record, origin, bundle)
    install_fetch(monkeypatch, record, origin)
    mock_official(monkeypatch, bundle)
    first = prepare_primary_evidence(settings, collect_requests(settings)[0][0])
    set_clock(monkeypatch, datetime.fromisoformat('2026-10-09T04:00:00+00:00'))
    retry = prepare_primary_evidence(settings, collect_requests(settings)[0][0])
    assert first.prompt_data() == retry.prompt_data()


def test_media_resume_holds_retracted_input_without_paid_call(tmp_path, monkeypatch):
    from blogbot.pipeline import _complete_media
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    register(settings, record, origin, bundle)
    install_fetch(monkeypatch, record, origin)
    mock_official(monkeypatch, bundle)
    request = prepare_primary_evidence(settings, collect_requests(settings)[0][0])
    post = replace(plan_draft(settings, request.id), provenance={**request.provenance,
                   'daily_plan': settings.config['daily_plan']})
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda *a: pytest.fail('Paid resume'))
    record['score']['fatal'] = ['withdrawn']
    with connect_db(settings.db_path) as conn:
        result = _complete_media(settings, conn, 1, post, request, {'total': 27})
    assert result['status'] == 'RESEARCH_REQUIRED'


def test_encrypted_transport_and_ready_recheck_preserve_evidence(tmp_path, monkeypatch):
    from dataclasses import asdict

    from cryptography.fernet import Fernet

    from blogbot.cloud import extract_bundle, filter_ready, pack
    from blogbot.core import save_post
    from blogbot.images import atomic_json

    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    register(settings, record, origin, bundle)
    install_fetch(monkeypatch, record, origin)
    mock_official(monkeypatch, bundle)
    request = prepare_primary_evidence(settings, collect_requests(settings)[0][0])
    request.provenance['daily_plan'] = settings.config['daily_plan']
    post = replace(plan_draft(settings, request.id), provenance=request.provenance)
    with connect_db(settings.db_path) as conn:
        ident = save_post(conn, post)
    path = settings.artifact_dir / f'2026-10-09-{ident:05d}.json'
    atomic_json(path, {'post': asdict(post), 'input': asdict(request), 'review': {'total': 27}})
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    encrypted = tmp_path / 'handoff.enc'
    pack(settings, encrypted)
    target = tmp_path / 'restored'
    extract_bundle(encrypted.read_bytes(), target, key)
    assert list((target / 'policy-evidence').glob('*.json'))
    ready = json.loads((target / 'ready.json').read_text())
    assert len(ready['posts']) == 1
    receipts = {'verified_date': '2026-10-09', 'records': []}
    record['score']['fatal'] = ['withdrawn']
    restored = replace(settings, db_path=target / 'blog.db', artifact_dir=target / 'drafts',
                       inbox_dir=target / 'inbox')
    filter_ready(target, receipts, settings.config['daily_plan'], restored)
    assert json.loads((target / 'ready.json').read_text())['posts'] == []


def test_optional_runner_metadata_is_bounded_and_never_a_candidate(tmp_path, monkeypatch):
    from blogbot.weekly_policy import import_environment
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    assert import_environment(settings, '') is None
    assert import_environment(settings, json.dumps(bundle))['candidate_created'] is False
    with pytest.raises(ValueError, match='bounded'):
        import_environment(settings, ' ' * 48001)
    install_fetch(monkeypatch, record, origin)
    record['score']['fatal'] = ['not approved']
    assert not collect_requests(settings)[0]


@pytest.mark.parametrize('market_date', ['20260507', '2026.05.07', '2026년 5월 7일', '2026-10-10'])
def test_market_date_forms_cannot_be_refreshed_by_metadata(tmp_path, monkeypatch, market_date):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    record['facts'] += f'\n{market_date} 종가 1000원'
    record['market_as_of_date'] = '2026-10-09'
    register(settings, record, origin, bundle)
    with pytest.raises(ValueError, match='market facts'):
        evidence_for(settings, record, origin)


def test_duplicate_canonical_source_withdrawal_suppresses_entire_group(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, _ = fixture(settings)
    withdrawn = {**record, 'id': 'new-id', 'src': record['src'] + '#withdrawn', 'withdrawn': True}
    record['src'] += '#text'
    commits = [{'sha': 'a' * 40, 'commit': {'committer': {'date': '2026-10-08T22:00:00Z'}}}]
    monkeypatch.setattr(inputs, '_get_json', lambda _: commits)
    assert inputs.fetch_policy_history(settings.config['community'], [record, withdrawn], origin) == []


def test_pack_reports_final_source_hold_in_summary(tmp_path, monkeypatch):
    from dataclasses import asdict

    from cryptography.fernet import Fernet

    from blogbot.cloud import pack
    from blogbot.core import save_post
    from blogbot.images import atomic_json

    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    register(settings, record, origin, bundle)
    install_fetch(monkeypatch, record, origin)
    mock_official(monkeypatch, bundle)
    request = prepare_primary_evidence(settings, collect_requests(settings)[0][0])
    request.provenance['daily_plan'] = settings.config['daily_plan']
    post = replace(plan_draft(settings, request.id), provenance=request.provenance)
    with connect_db(settings.db_path) as conn:
        ident = save_post(conn, post)
    path = settings.artifact_dir / f'2026-10-09-{ident:05d}.json'
    atomic_json(path, {'post': asdict(post), 'input': asdict(request), 'review': {'total': 27}})
    atomic_json(settings.db_path.parent / 'run-summary.json', {'date': '2026-10-09',
                'failed': False, 'results': [{'id': ident, 'status': 'APPROVED'}]})
    monkeypatch.setenv('BLOG_BUNDLE_KEY', Fernet.generate_key().decode())
    record['score']['fatal'] = ['withdrawn']
    assert pack(settings, tmp_path / 'handoff.enc') == 'weekly_policy_revalidation_required'
    summary = json.loads((tmp_path / 'run-summary.json').read_text())
    assert summary['failed'] and summary['results'][0]['status'] == 'WEEKLY_POLICY_REVALIDATION_REQUIRED'
    assert json.loads((tmp_path / 'ready.json').read_text())['posts'] == []


def test_previous_project_cannot_be_promoted_to_new_participation(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    bundle['company_links'][0]['relationship'] = 'official_participation'
    register(settings, record, origin, bundle)
    with pytest.raises(ValueError, match='Earlier company'):
        evidence_for(settings, record, origin)


def test_default_https_port_is_same_event_identity():
    from blogbot.weekly_policy import source_identity
    assert source_identity('https://www.mohw.go.kr:443/doc?id=1#text') == source_identity(
        'https://www.mohw.go.kr/doc?id=1#other')


def test_policy_event_cannot_postdate_its_original_export(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record, origin, bundle = fixture(settings)
    origin['snapshot_date'] = '2026-10-08'
    assert community_request(record, origin, settings.config['community'], policy_evidence=bundle) is None
