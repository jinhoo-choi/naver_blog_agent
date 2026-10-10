"""Offline synthetic fixtures. Never log in, fetch private APIs, or buy model calls."""
import copy
import io
import json
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from blogbot import creator_trends
from blogbot.config import load_settings
from blogbot.core import connect_db, today_kst
from blogbot.creator_trends import DECISIONS, KST, REPORT, SNAPSHOT, consult, validate_snapshot
from blogbot.images import atomic_json
from blogbot.inputs import ContentRequest
from blogbot.topics import rank_candidates

NOW = datetime(2026, 10, 6, 8, 55, tzinfo=KST)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
    monkeypatch.setattr('blogbot.core.datetime', Clock)
    monkeypatch.setattr('blogbot.creator_trends.datetime', Clock)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    monkeypatch.delenv('BLOG_CREATOR_TRENDS_JSON', raising=False)
    monkeypatch.delenv('NAVER_API_HUB_CLIENT_ID', raising=False)
    monkeypatch.delenv('NAVER_API_HUB_CLIENT_SECRET', raising=False)
    result = replace(load_settings(), root=tmp_path / 'repository')
    result.config['community']['enabled'] = False
    result.config['topics']['enabled'] = False
    return result


def snapshot(category='parenting', keyword='수면시간', metric='search_inflow_rank'):
    return {'version': creator_trends.VERSION, 'channel_id': 'choijku',
            'capture_method': 'authenticated_ui', 'captured_at': NOW.isoformat(),
            'groups': [{'category': category,
                        'ui_category': {'parenting': '육아·결혼', 'exercise': '스포츠',
                                        'investment': '비즈니스·경제'}[category],
                        'metric': metric, 'data_date': '2026-10-05',
                        'keyword': keyword if metric == 'search_inflow_rank' else '',
                        'source_url': 'https://creator-advisor.naver.com/naver_blog/choijku/trends',
                        'entries': [{'rank': 1, 'title': f'{keyword} 조건 비교 (synthetic fixture)',
                                     'url': 'https://blog.naver.com/fixture/123', 'views': None}]}]}


def request(identity, keyword, category='parenting', **data):
    return ContentRequest(identity, category, {'question': keyword, 'benchmark_query': keyword, **data})


def run(settings, candidates):
    with closing(connect_db(settings.db_path)) as conn:
        return rank_candidates(settings, conn, candidates, 1)


def test_imported_evidence_changes_selection_before_reservation(settings, monkeypatch):
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot()))
    result = run(settings, [request('unmatched', '기저귀'), request('matched', '수면시간')])
    assert [r.id for r in result] == ['matched', 'unmatched']
    decision = result[0].provenance['creator_trends']
    assert decision['status'] == 'CONSULTED'
    assert decision['matches'][0]['views'] is None
    assert decision['matches'][0]['data_date'] == '2026-10-05'
    assert decision['matches'][0]['ui_category'] == '육아·결혼'
    with closing(connect_db(settings.db_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
    report = json.loads((settings.db_path.parent / REPORT).read_text())
    assert report['network_calls'] == report['paid_calls'] == 0
    assert report['order'] == ['matched', 'unmatched']


def test_unavailable_preserves_queue_and_records_truth_even_single_candidate(settings):
    result = run(settings, [request('one', '수면시간')])
    assert result[0].provenance['creator_trends']['status'] == 'UNAVAILABLE'
    assert result[0].provenance['creator_trends']['matches'] == []
    assert result[0].provenance['creator_trends']['article_match_count'] == 0


@pytest.mark.parametrize('bad,expected', [
    ('bad json', 'INVALID'),
    ({'captured_at': (NOW - timedelta(hours=37)).isoformat()}, 'EXPIRED'),
    ({'captured_at': (NOW + timedelta(hours=1)).isoformat()}, 'FUTURE_CAPTURE'),
    ({'channel_id': 'different'}, 'INVALID'),  # URL and channel mismatch.
])
def test_invalid_environment_without_valid_alternative_is_reported(settings, monkeypatch, bad, expected):
    evidence = snapshot()
    evidence['groups'][0]['data_date'] = '2026-10-04'
    raw = bad if isinstance(bad, str) else json.dumps({**evidence, **bad})
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', raw)
    result = run(settings, [request('one', '수면시간')])
    assert result[0].provenance['creator_trends']['status'] == expected


@pytest.mark.parametrize('source', ['environment', 'private_snapshot', 'repository_snapshot'])
def test_newest_valid_source_wins_and_preserves_observation_time(
        settings, monkeypatch, capsys, source):
    (settings.root / 'config').mkdir(parents=True)
    for index, name in enumerate(['environment', 'private_snapshot', 'repository_snapshot']):
        evidence = snapshot()
        captured = NOW if name == source else NOW - timedelta(hours=index + 1)
        evidence['captured_at'] = captured.isoformat()
        evidence['groups'][0]['entries'][0]['title'] = f'수면시간 {name}'
        if name == 'environment':
            monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(evidence))
        else:
            path = (settings.db_path.parent if name == 'private_snapshot'
                    else settings.root / 'config') / SNAPSHOT
            atomic_json(path, evidence)
    decision = run(settings, [request('one', '수면시간')])[0].provenance['creator_trends']
    assert decision['status'] == 'CONSULTED'
    assert decision['captured_at'] == NOW.isoformat()
    assert decision['matches'][0]['title'] == f'수면시간 {source}'
    assert json.loads((settings.db_path.parent / SNAPSHOT).read_text())['captured_at'] == NOW.isoformat()
    assert f'CREATOR_TRENDS_SOURCE_SELECTED source={source}' in capsys.readouterr().out


@pytest.mark.parametrize('bad,reason', [
    ('bad json', 'INVALID'),
    ({'captured_at': (NOW - timedelta(hours=37)).isoformat()}, 'EXPIRED'),
    ({'captured_at': (NOW + timedelta(hours=1)).isoformat()}, 'FUTURE_CAPTURE'),
])
@pytest.mark.parametrize('source', ['private_snapshot', 'repository_snapshot'])
def test_invalid_environment_does_not_shadow_valid_observed_file(
        settings, monkeypatch, capsys, source, bad, reason):
    directory = settings.db_path.parent if source == 'private_snapshot' else settings.root / 'config'
    directory.mkdir(parents=True, exist_ok=True)
    evidence = snapshot()
    atomic_json(directory / SNAPSHOT, evidence)
    invalid = snapshot()
    invalid['groups'][0]['data_date'] = '2026-10-04'
    raw = bad if isinstance(bad, str) else json.dumps({**invalid, **bad})
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', raw)
    decision = run(settings, [request('one', '수면시간')])[0].provenance['creator_trends']
    assert decision['status'] == 'CONSULTED'
    assert decision['captured_at'] == evidence['captured_at']
    assert f'source=environment status={reason}' in capsys.readouterr().out


@pytest.mark.parametrize('change,reason', [
    (lambda s: s.update(enabled=False), 'INVALID'),
    (lambda s: s.update(captured_at=(NOW + timedelta(hours=1)).isoformat()), 'FUTURE_CAPTURE'),
    (lambda s: s['groups'][0].update(data_date='2026-10-01'), 'EXPIRED_DATA'),
    (lambda s: (s.update(channel_id='other'), s['groups'][0].update(
        source_url='https://creator-advisor.naver.com/naver_blog/other/trends')), 'WRONG_CHANNEL'),
])
def test_invalid_repository_source_cannot_replace_valid_evidence(
        settings, monkeypatch, capsys, change, reason):
    (settings.root / 'config').mkdir(parents=True)
    valid = snapshot()
    valid['captured_at'] = (NOW - timedelta(hours=1)).isoformat()
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(valid))
    invalid = snapshot()
    change(invalid)
    atomic_json(settings.root / 'config' / SNAPSHOT, invalid)
    decision = run(settings, [request('one', '수면시간')])[0].provenance['creator_trends']
    assert decision['status'] == 'CONSULTED'
    assert decision['captured_at'] == valid['captured_at']
    assert f'source=repository_snapshot status={reason}' in capsys.readouterr().out


def test_repository_snapshot_keeps_each_group_data_date_and_never_adds_inputs(settings):
    (settings.root / 'config').mkdir(parents=True)
    evidence = snapshot()
    old = copy.deepcopy(evidence['groups'][0])
    old['data_date'] = '2026-10-01'
    old['keyword'] = '기저귀'
    old['entries'][0]['title'] = '기저귀 오래된 근거'
    evidence['groups'].append(old)
    atomic_json(settings.root / 'config' / SNAPSHOT, evidence)
    assert run(settings, []) == []
    decision = run(settings, [request('one', '기저귀')])[0].provenance['creator_trends']
    assert decision['matches'] == []
    assert decision['status'] == 'NO_RELEVANT_MATCH'
    cached = json.loads((settings.db_path.parent / SNAPSHOT).read_text())
    assert [group['data_date'] for group in cached['groups']] == ['2026-10-05', '2026-10-01']


def test_refreshing_capture_does_not_refresh_old_data(settings, monkeypatch):
    evidence = snapshot()
    evidence['groups'][0]['data_date'] = '2026-10-01'
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(evidence))
    result = run(settings, [request('one', '수면시간')])
    assert result[0].provenance['creator_trends']['status'] == 'EXPIRED_DATA'


def test_missing_category_and_irrelevant_topics_are_not_promoted(settings, monkeypatch):
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot('exercise', '벤치프레스')))
    result = run(settings, [request('one', '수면시간')])
    assert result[0].provenance['creator_trends']['status'] == 'CATEGORY_UNAVAILABLE'
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot(keyword='행사')))
    result = run(settings, [request('one', '수면시간'), request('two', '행사장')])
    assert [r.id for r in result] == ['one', 'two']
    assert all(r.provenance['creator_trends']['status'] == 'NO_RELEVANT_MATCH' for r in result)


def test_keyword_screenshot_is_not_article_or_view_evidence(settings, monkeypatch):
    evidence = snapshot(metric='keyword_inflow_order')
    evidence['capture_method'] = 'owner_screenshot'
    evidence['groups'][0]['entries'][0]['url'] = (
        'https://creator-advisor.naver.com/new-windows/trend-stats?query=sleep')
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(evidence))
    result = run(settings, [request('other', '기저귀'), request('one', '수면시간')])
    assert [r.id for r in result] == ['other', 'one']
    assert result[1].provenance['creator_trends']['status'] == 'KEYWORDS_ONLY'
    assert result[1].provenance['creator_trends']['article_match_count'] == 0


@pytest.mark.parametrize('change', [
    lambda s: s['groups'][0]['entries'][0].update(views=26),
    lambda s: s['groups'][0]['entries'][0].update(rank=True),
    lambda s: s['groups'][0]['entries'][0].update(url='https://evil.example/blog/1'),
    lambda s: s['groups'][0].update(ui_category='스타·연예인'),
    lambda s: s['groups'][0].update(metric='views'),
    lambda s: s['groups'][0].update(source_url='https://creator-advisor.naver.com/private/api'),
    lambda s: s.update(captured_at='2026-10-06T08:55:00'),
    lambda s: s['groups'][0].update(data_date='2026-10-07'),
    lambda s: s['groups'][0]['entries'].append(copy.deepcopy(s['groups'][0]['entries'][0])),
    lambda s: s['groups'][0]['entries'][0].update(title='x' * 251),
])
def test_rejects_unobserved_metrics_and_malformed_input(change):
    evidence = snapshot()
    change(evidence)
    with pytest.raises((ValueError, TypeError)):
        validate_snapshot(json.dumps(evidence))


def test_duplicate_risk_cannot_win_and_does_not_delete_user_input(settings, monkeypatch):
    atomic_json(settings.db_path.parent / 'context.json', {'published_titles': ['수면시간']})
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot()))
    result = run(settings, [request('new', '기저귀'), request('overlap', '수면시간')])
    assert [r.id for r in result] == ['new', 'overlap']
    assert result[1].provenance['creator_trends']['duplicate_title_risk'] is True


def test_owner_priority_and_investment_kind_survive_trends(settings, monkeypatch):
    evidence = snapshot('investment', '인기기업')
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(evidence))
    scheduled = request('owner', '알테오젠', 'investment', kind='research')
    settings.config['topics']['scheduled'] = {str(today_kst()): {'id': 'owner'}}
    result = run(settings, [scheduled, request('policy', '산업정책', 'investment', kind='policy'),
                           request('viral', '인기기업', 'investment', kind='disclosure')])
    assert [r.id for r in result] == ['owner', 'policy', 'viral']
    assert result[0].provenance['creator_trends']['priority_preserved'] is True


def test_retry_keeps_exact_evidence_and_no_new_paid_work(settings, monkeypatch):
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot()))
    first = run(settings, [request('one', '수면시간')])[0]
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), 'parenting', 'one', 'ERROR'))
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', 'invalid replacement')
    again = run(settings, [request('one', '수면시간')])[0]
    assert again.provenance == first.provenance
    assert json.loads((settings.db_path.parent / DECISIONS).read_text())['decisions']['one'] == (
        first.provenance['creator_trends'])


def test_legacy_retry_is_not_retagged(settings, monkeypatch):
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), 'parenting', 'old', 'ERROR'))
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot()))
    result = run(settings, [request('old', '수면시간')])
    assert 'creator_trends' not in result[0].provenance


def test_cli_import_and_encrypted_checkpoint_transport(settings, tmp_path, monkeypatch):
    from blogbot.cloud import pack

    source = tmp_path / 'observed.json'
    atomic_json(source, snapshot())
    assert creator_trends.import_snapshot(settings, source)['status'] == 'CREATOR_TRENDS_IMPORTED'
    run(settings, [request('one', '수면시간')])
    key = Fernet.generate_key()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key.decode())
    settings.artifact_dir.mkdir(exist_ok=True)
    bundle = tmp_path / 'bundle.enc'
    pack(settings, bundle)
    with zipfile.ZipFile(io.BytesIO(Fernet(key).decrypt(bundle.read_bytes()))) as archive:
        assert {SNAPSHOT, REPORT, DECISIONS} <= set(archive.namelist())
        assert json.loads(archive.read(SNAPSHOT))['channel_id'] == 'choijku'
    before = (settings.db_path.parent / SNAPSHOT).read_bytes()
    source.write_text('{invalid')
    with pytest.raises(ValueError):
        creator_trends.import_snapshot(settings, source)
    assert (settings.db_path.parent / SNAPSHOT).read_bytes() == before


def test_pipeline_wiring_precedes_reservation_and_paid_preparation():
    root = Path(__file__).resolve().parents[1]
    pipeline = (root / 'src/blogbot/pipeline.py').read_text()
    assert pipeline.index('candidates = rank_candidates(') < pipeline.index('else reserve_attempt(')
    assert pipeline.index('candidates = rank_candidates(') < pipeline.index('request = prepare_request(')
    workflow = (root / '.github/workflows/blog-prepare.yml').read_text()
    assert 'BLOG_CREATOR_TRENDS_JSON: ${{ vars.BLOG_CREATOR_TRENDS_JSON }}' in workflow
    assert 'creator_trends_status' in pipeline
    for name in ['writer', 'reviewer']:
        assert 'views=null' in (root / f'prompts/{name}.md').read_text()


def test_disabled_feature_does_not_consult_or_change_requests(settings, monkeypatch):
    settings.config['creator_advisor']['enabled'] = False
    monkeypatch.setattr(creator_trends, '_load', lambda *args: pytest.fail('Do not load'))
    candidates = [request('one', '수면시간')]
    assert consult(settings, None, candidates, 1) is candidates


def test_import_creates_fresh_private_state_without_creating_db(settings, tmp_path):
    from dataclasses import replace

    source = tmp_path / 'snapshot.json'
    atomic_json(source, snapshot())
    fresh = replace(settings, db_path=tmp_path / 'new-state' / 'blog.db')
    creator_trends.import_snapshot(fresh, source)
    assert (fresh.db_path.parent / SNAPSHOT).exists()
    assert not fresh.db_path.exists()


def test_decision_write_failure_preserves_original_without_new_provenance(settings, monkeypatch, capsys):
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot()))
    (settings.db_path.parent / DECISIONS).mkdir()
    result = run(settings, [request('other', '기저귀'), request('one', '수면시간')])
    assert [r.id for r in result] == ['other', 'one']
    assert all('creator_trends' not in r.provenance for r in result)
    assert 'decision_write_failed' in capsys.readouterr().out


def test_report_write_failure_is_optional_and_keeps_replayable_decision(settings, monkeypatch, capsys):
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot()))
    (settings.db_path.parent / REPORT).mkdir()
    result = run(settings, [request('one', '수면시간')])
    assert result[0].provenance['creator_trends']['status'] == 'CONSULTED'
    assert (settings.db_path.parent / DECISIONS).is_file()
    assert 'report_write_failed' in capsys.readouterr().out


def test_bounded_provenance_retains_article_support_ahead_of_keyword_rows(settings, monkeypatch):
    evidence = snapshot(metric='keyword_inflow_order')
    group = evidence['groups'][0]
    group['entries'] = [{'rank': i, 'title': f'수면시간 조건 {i}', 'views': None,
                         'url': f'https://creator-advisor.naver.com/new-windows/trend-stats?query={i}'}
                        for i in range(1, 7)]
    evidence['groups'].append(snapshot()['groups'][0])
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(evidence))
    decision = run(settings, [request('one', '수면시간')])[0].provenance['creator_trends']
    assert decision['status'] == 'CONSULTED' and len(decision['matches']) == 5
    assert decision['matches'][0]['metric'] == 'search_inflow_rank'


def test_disabled_example_cannot_be_imported():
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError, match='before enabling'):
        validate_snapshot((root / 'config/creator-trends.example.json').read_text())


def test_failed_decision_rewrite_keeps_prior_paid_request_provenance(settings, monkeypatch):
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot()))
    first = run(settings, [request('one', '수면시간')])[0]
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), 'parenting', 'one', 'ERROR'))
    real = creator_trends.atomic_json
    def write(path, value):
        if path.name == DECISIONS:
            raise OSError('fixture: cannot replace decisions')
        real(path, value)
    monkeypatch.setattr(creator_trends, 'atomic_json', write)
    again = run(settings, [request('one', '수면시간'), request('new', '기저귀')])
    assert again[0].provenance == first.provenance
    assert 'creator_trends' not in again[1].provenance


def test_failed_replace_holds_unattempted_old_sidecar_without_tainting_new_work(settings, monkeypatch):
    monkeypatch.setenv('BLOG_CREATOR_TRENDS_JSON', json.dumps(snapshot()))
    run(settings, [request('old-shortlist', '수면시간')])
    real = creator_trends.atomic_json
    def write(path, value):
        if path.name == DECISIONS:
            raise OSError('fixture: old sidecar cannot be cleared')
        real(path, value)
    monkeypatch.setattr(creator_trends, 'atomic_json', write)
    result = run(settings, [request('old-shortlist', '수면시간'), request('new', '기저귀')])
    assert [r.id for r in result] == ['new']
    assert 'creator_trends' not in result[0].provenance
