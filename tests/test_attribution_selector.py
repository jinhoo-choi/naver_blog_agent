"""Offline coverage for the explicitly selected, attribution-only recovery route."""
import inspect
import json
import re
import socket
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from blogbot import cloud, pipeline, recovery


def forbidden(*args, **kwargs):
    pytest.fail('Selected attribution review must not perform this side effect')


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr('blogbot.notify.telegram', lambda *args, **kwargs: None)


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(
        root=tmp_path,
        db_path=tmp_path / 'blog.db',
        artifact_dir=tmp_path / 'drafts',
        daily_count=1,
        config={
            'blog': {'daily_min': 1, 'daily_max': 1},
            'categories': {'exercise': {'max_daily': 1}},
        },
    )


def forbid_candidate_work(monkeypatch):
    for name in (
        'collect_requests', 'rank_candidates', 'reserve_attempt', 'BlogLLM',
        'make_writer', 'save_pending', 'prepare_request', 'prepare_primary_evidence',
        'run_pre_review', 'resume_candidate', 'generate_images', 'save_post',
    ):
        monkeypatch.setattr(pipeline, name, forbidden)
    monkeypatch.setattr(recovery, 'recover_rejected', forbidden)


def test_pipeline_selector_defaults_to_disabled():
    parameter = inspect.signature(pipeline.run_daily).parameters['attribution_review_request_id']
    assert parameter.default == ''


@pytest.mark.parametrize('explicit_empty', [False, True])
def test_empty_selector_preserves_normal_candidate_route(settings, monkeypatch, explicit_empty):
    calls = []
    monkeypatch.setattr(pipeline, 'active_plan', lambda _: None)
    monkeypatch.setattr(pipeline, 'collect_requests', lambda _: calls.append('collect') or ([], []))
    monkeypatch.setattr(
        pipeline, 'rank_candidates', lambda *args: calls.append('rank') or [])
    monkeypatch.setattr(recovery, 'recover_attribution_review', forbidden)
    kwargs = {'attribution_review_request_id': ''} if explicit_empty else {}

    assert pipeline.run_daily(settings, **kwargs) == [
        {'status': 'NO_ELIGIBLE_INPUT_OR_DAILY_LIMIT'}]
    assert calls == ['collect', 'rank']


@pytest.mark.parametrize('request_id', ['A', 'facepull_2026-10-08', 'A' + 'x' * 79])
def test_selected_pipeline_delegates_once_without_candidate_work(
        settings, monkeypatch, request_id):
    forbid_candidate_work(monkeypatch)
    monkeypatch.setattr(pipeline, 'active_plan', forbidden)
    calls = []
    expected = [{'request_id': request_id, 'status': 'DROP_REVIEW'}]

    def recover(selected_settings, conn, selected_id):
        assert selected_settings is settings
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 0
        calls.append(selected_id)
        return expected[0]

    monkeypatch.setattr(recovery, 'recover_attribution_review', recover)
    result = pipeline.run_daily(
        settings, count=999, retry_failed=True, save_to_naver=False,
        attribution_review_request_id=request_id)

    assert result == expected
    assert calls == [request_id]
    assert not settings.artifact_dir.exists()


INVALID_SELECTORS = [
    '../facepull', '/facepull', 'face/pull', 'face.pull', '-facepull', '_facepull',
    'face pull', 'facepull\n', ' facepull', 'facepull ', '페이스풀',
    'a' * 81, 'facepull;echo injected', '$(echo injected)',
]


@pytest.mark.parametrize('request_id', [*INVALID_SELECTORS, None, 0, [], {}])
def test_invalid_selector_fails_before_pipeline_side_effects(settings, monkeypatch, request_id):
    forbid_candidate_work(monkeypatch)
    monkeypatch.setattr(pipeline, 'active_plan', forbidden)
    monkeypatch.setattr(pipeline, 'connect_db', forbidden)
    monkeypatch.setattr(recovery, 'recover_attribution_review', forbidden)

    with pytest.raises(ValueError):
        pipeline.run_daily(settings, retry_failed=True, attribution_review_request_id=request_id)
    assert not settings.db_path.exists()
    assert not settings.artifact_dir.exists()


@pytest.mark.parametrize('retry_failed,save_to_naver', [
    (False, False), (False, True), (True, True),
])
def test_selector_requires_recovery_without_naver_save(
        settings, monkeypatch, retry_failed, save_to_naver):
    forbid_candidate_work(monkeypatch)
    monkeypatch.setattr(pipeline, 'active_plan', forbidden)
    monkeypatch.setattr(pipeline, 'connect_db', forbidden)
    monkeypatch.setattr(recovery, 'recover_attribution_review', forbidden)

    with pytest.raises(ValueError):
        pipeline.run_daily(
            settings, retry_failed=retry_failed, save_to_naver=save_to_naver,
            attribution_review_request_id='facepull')
    assert not settings.db_path.exists()


@pytest.mark.parametrize('mode', ['prepare', 'bootstrap', 'probe', 'unpack'])
def test_cloud_rejects_selector_in_other_modes_before_side_effects(monkeypatch, mode):
    monkeypatch.setattr('sys.argv', [
        'cloud', mode, '--attribution-review-request-id', 'facepull'])
    for name in ('load_settings', 'restore', 'seed_inputs', 'pack', 'run_daily', 'github_get'):
        monkeypatch.setattr(cloud, name, forbidden)

    with pytest.raises(SystemExit) as error:
        cloud.main()
    assert error.value.code == 2


@pytest.mark.parametrize('request_id', INVALID_SELECTORS)
def test_cloud_rejects_invalid_selector_before_side_effects(monkeypatch, request_id):
    monkeypatch.setattr('sys.argv', [
        'cloud', 'recover', '--attribution-review-request-id=' + request_id])
    for name in ('load_settings', 'restore', 'seed_inputs', 'pack', 'run_daily', 'github_get'):
        monkeypatch.setattr(cloud, name, forbidden)

    with pytest.raises(SystemExit) as error:
        cloud.main()
    assert error.value.code == 2


def setup_cloud(settings, monkeypatch, argv):
    monkeypatch.setattr('sys.argv', ['cloud', *argv])
    monkeypatch.setattr(cloud, 'load_settings', lambda: settings)
    monkeypatch.setattr(cloud, 'active_plan', lambda _: None)
    events = []
    monkeypatch.setattr(cloud, 'restore', lambda _: events.append('restore'))
    monkeypatch.setattr(cloud, 'pack', lambda *args: events.append('pack'))
    return events


@pytest.mark.parametrize('mode', ['prepare', 'recover'])
@pytest.mark.parametrize('explicit_empty', [False, True])
def test_cloud_empty_selector_preserves_seed_policy_and_automatic_recovery(
        settings, monkeypatch, mode, explicit_empty):
    argv = [mode] + (['--attribution-review-request-id', ''] if explicit_empty else [])
    events = setup_cloud(settings, monkeypatch, argv)
    monkeypatch.setattr(cloud, 'seed_inputs', lambda _: events.append('seed'))
    monkeypatch.setattr('blogbot.weekly_policy.import_environment',
                        lambda *args: events.append('policy'))
    monkeypatch.setattr(cloud, 'ready_categories', lambda *args: {'exercise'})
    calls = []
    expected = [{'request_id': 'ordinary', 'category': 'exercise', 'status': 'APPROVED'}]

    def run(*args, **kwargs):
        calls.append(kwargs)
        return expected.copy()

    def automatic_recovery(selected_settings, results, count):
        assert selected_settings is settings and count == 1
        events.append('automatic_recovery')
        return results

    monkeypatch.setattr(cloud, 'run_daily', run)
    monkeypatch.setattr(cloud, 'recover_preparation', automatic_recovery)
    cloud.main()

    assert events == ['restore', 'seed', 'policy', 'automatic_recovery', 'pack']
    assert len(calls) == 1
    assert calls[0]['retry_failed'] is (mode == 'recover')
    assert calls[0]['save_to_naver'] is False
    assert not calls[0].get('attribution_review_request_id')
    summary = json.loads((settings.db_path.parent / 'run-summary.json').read_text())
    assert summary['failed'] is False
    assert summary['results'] == expected


@pytest.mark.parametrize('status,failed', [
    ('APPROVED', False), ('DROP_REVIEW', True), ('ERROR', True),
])
def test_cloud_selection_skips_unrelated_work_but_packs_and_summarizes(
        settings, monkeypatch, status, failed):
    request_id = 'facepull_selected-only'
    events = setup_cloud(settings, monkeypatch, [
        'recover', '--attribution-review-request-id', request_id])
    for name in ('seed_inputs', 'recover_preparation', 'ready_categories', 'connect_db'):
        monkeypatch.setattr(cloud, name, forbidden)
    monkeypatch.setattr(cloud, 'import_manual_saves', lambda _: events.append('receipts'))
    monkeypatch.setattr('blogbot.weekly_policy.import_environment', forbidden)
    calls = []
    expected = [{'request_id': request_id, 'category': 'exercise', 'status': status}]

    def run(selected_settings, **kwargs):
        assert selected_settings is settings
        events.append('target')
        calls.append(kwargs)
        return expected.copy()

    monkeypatch.setattr(cloud, 'run_daily', run)
    if failed:
        with pytest.raises(cloud.PreparationFailed):
            cloud.main()
    else:
        cloud.main()

    assert events == ['restore', 'receipts', 'target', 'pack']
    assert len(calls) == 1
    assert calls[0]['retry_failed'] is True
    assert calls[0]['save_to_naver'] is False
    assert calls[0]['attribution_review_request_id'] == request_id
    summary = json.loads((settings.db_path.parent / 'run-summary.json').read_text())
    assert summary['failed'] is failed
    assert summary['results'] == expected
    assert not (settings.db_path.parent / 'auto-recovery.json').exists()


def test_selected_cloud_imports_real_save_receipt_before_target_quota_gate(settings, monkeypatch):
    from blogbot.core import connect_db, today_kst
    from blogbot.planning import saved_count

    request_id = 'facepull_selected-only'
    saved_id = 'already-saved-offline'
    day = today_kst().isoformat()
    config_dir = settings.root / 'config'
    config_dir.mkdir()
    (config_dir / 'manual-saves.json').write_text(json.dumps({'records': [{
        'request_id': saved_id, 'day': day, 'category': 'exercise', 'status': 'SAVED_NAVER',
    }]}))
    events = setup_cloud(settings, monkeypatch, [
        'recover', '--attribution-review-request-id', request_id])
    for name in ('seed_inputs', 'recover_preparation', 'ready_categories'):
        monkeypatch.setattr(cloud, name, forbidden)
    monkeypatch.setattr('blogbot.weekly_policy.import_environment', forbidden)
    forbid_candidate_work(monkeypatch)

    def restore_without_latest_receipt(directory):
        assert directory == settings.db_path.parent
        with closing(connect_db(settings.db_path)) as conn, conn:
            conn.executemany(
                'INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                [(day, 'exercise', saved_id, 'ERROR'),
                 (day, 'exercise', request_id, 'DROP_REVIEW')])
            assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 0
        events.append('restore')

    import_receipts = cloud.import_manual_saves

    def receipt_import(selected_settings):
        assert events == ['restore']
        import_receipts(selected_settings)
        events.append('receipts')

    expected = [{'request_id': request_id, 'status': 'DAILY_PLAN_LIMIT'}]

    def target_quota_gate(selected_settings, **kwargs):
        assert selected_settings is settings
        assert events == ['restore', 'receipts']
        assert kwargs['attribution_review_request_id'] == request_id
        assert kwargs['retry_failed'] is True and kwargs['save_to_naver'] is False
        with closing(connect_db(settings.db_path)) as conn:
            assert saved_count(conn, {'date': day}) == 1
            receipt = conn.execute(
                'SELECT request_id,day,category FROM save_receipts').fetchone()
            assert tuple(receipt) == (saved_id, day, 'exercise')
            attempts = dict(conn.execute('SELECT request_id,status FROM attempts').fetchall())
            assert attempts == {saved_id: 'SAVED_NAVER', request_id: 'DROP_REVIEW'}
            assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 0
        events.append('target')
        return expected.copy()

    monkeypatch.setattr(cloud, 'restore', restore_without_latest_receipt)
    monkeypatch.setattr(cloud, 'import_manual_saves', receipt_import)
    monkeypatch.setattr(cloud, 'run_daily', target_quota_gate)
    cloud.main()

    assert events == ['restore', 'receipts', 'target', 'pack']
    summary = json.loads((settings.db_path.parent / 'run-summary.json').read_text())
    assert summary['failed'] is False and summary['results'] == expected
    assert not (settings.db_path.parent / 'context.json').exists()
    assert not (settings.db_path.parent / 'auto-recovery.json').exists()
    assert not settings.artifact_dir.exists()


def test_workflow_selector_is_optional_and_quoted_without_permission_or_schedule_changes():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / '.github/workflows/blog-prepare.yml').read_text()
    input_match = re.search(
        r'^      attribution_review_request_id:\n((?:^        .*\n)+)',
        workflow, re.MULTILINE)
    assert input_match is not None
    assert 'type: string' in input_match[1]
    assert re.search(r"default: (?:''|\"\")", input_match[1])
    assert 'required: true' not in input_match[1]

    environment_match = re.search(
        r'^          ([A-Z_]+): \$\{\{ inputs\.attribution_review_request_id'
        r"(?: \|\| '')? \}\}$", workflow, re.MULTILINE)
    assert environment_match is not None
    variable = environment_match[1]
    assert re.search(
        r'--attribution-review-request-id(?:\s+|=)"\$' + re.escape(variable) + r'"',
        workflow)
    run_lines = '\n'.join(line for line in workflow.splitlines() if 'run:' in line)
    assert '${{ inputs.attribution_review_request_id' not in run_lines
    assert 'permissions:\n  contents: read\n  actions: read\n' in workflow
    assert 'concurrency:\n  group: blog-api-prepare\n  cancel-in-progress: false\n' in workflow
    assert not re.search(r'^\s*(?:schedule:|-?\s*cron:)', workflow, re.MULTILINE)
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert 'github.actor == github.repository_owner' in workflow
    assert "(inputs.mode != 'prepare' || vars.BLOG_PREPARE_ENABLED == 'true')" in workflow
