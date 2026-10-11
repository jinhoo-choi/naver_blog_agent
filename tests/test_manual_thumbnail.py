"""Offline one-image preparation with separate cost consent and durable claims."""
import base64
import copy
import hashlib
import io
import json
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from openai import APIStatusError
from PIL import Image

from blogbot import cloud, manual_requests, manual_thumbnail
from blogbot.config import load_settings
from blogbot.core import connect_db
from blogbot.images import ImagePending
from blogbot.manual_costs import CostBoundUnavailable
from blogbot.planning import MediaBusy, media_claim

DAY = '2026-10-10'
REQUEST_ID = 'owner-origins-thumbnail-20261010'
SOURCE = 'https://stdict.korean.go.kr/search/searchView.do?word_no=123'
image_bytes = io.BytesIO()
Image.new('RGB', (32, 32), '#efe6c9').save(image_bytes, format='JPEG')
IMAGE = image_bytes.getvalue()


@pytest.fixture
def thumbnail(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 10, 10, 3, tzinfo=UTC)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr('blogbot.core.datetime', Clock)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-5')
    monkeypatch.setenv('OPENAI_REVIEW_MODEL', 'gpt-5')
    monkeypatch.setenv('BLOG_NOTIFY_ENABLED', 'false')
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
    settings = load_settings()
    with closing(connect_db(settings.db_path)):
        pass
    key = Fernet.generate_key()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key.decode())
    directory = settings.db_path.parent / 'manual-thumbnails' / REQUEST_ID
    packet = {
        'version': 'manual-thumbnail-v1', 'request_id': REQUEST_ID, 'date': DAY,
        'approval_reference': 'Sentinel_test_only_thumbnail_approval',
        'approved_by': 'repository_owner', 'scope': 'thumbnail_only', 'category': 'origins',
        'question': '공깃밥의 공기는 무슨 뜻인가요?',
        'title': '공깃밥의 공기는 무슨 뜻일까요?',
        'summary': '공기는 밥을 담아 먹는 작은 그릇을 뜻합니다.',
        'sources': [{'url': SOURCE, 'excerpt': '공기는 밥을 담아 먹는 작은 그릇입니다.'}],
        'budget': {'writer_model': 'gpt-5', 'review_model': 'gpt-5', 'max_calls': 4,
                   'max_output_tokens': 36000, 'budget_mode': 'estimated_with_call_caps',
                   'max_estimated_usd': 3,
                   'cost_approval_reference': 'Sentinel_test_only_estimated_cost_consent'},
    }
    calls, constructors, actions, remote_checks = [], [], [], []

    def generate(**kwargs):
        calls.append(kwargs)
        assert json.loads((directory / 'claim.json').read_text())['status'] == 'STARTED'
        assert json.loads((directory / 'paid-call.json').read_text())['status'] == 'STARTED'
        action = actions.pop(0) if actions else IMAGE
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, SimpleNamespace):
            return action
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(action).decode())],
                               usage=None, _request_id='offline-image-response')

    def make_client(**kwargs):
        constructors.append(kwargs)
        return SimpleNamespace(images=SimpleNamespace(generate=generate))

    monkeypatch.setattr('blogbot.images.OpenAI', make_client)
    monkeypatch.setattr('blogbot.llm.OpenAI',
                        lambda **kwargs: pytest.fail('Thumbnail mode cannot purchase text'))
    monkeypatch.setattr('blogbot.pipeline.make_writer',
                        lambda *args: pytest.fail('Thumbnail mode cannot write to Naver'))
    monkeypatch.setattr('blogbot.images.time.sleep',
                        lambda *args: pytest.fail('A single thumbnail never sleeps to retry'))

    def github_get(path):
        remote_checks.append(path)
        if path == '/actions/artifacts?per_page=100&page=1':
            return b'{"artifacts": []}'
        assert path == '/actions/runs/123/artifacts?per_page=100'
        return json.dumps({'artifacts': [
            {'name': manual_requests.reservation_name('123', packet, 'thumbnail'),
             'expired': False}]}).encode()

    monkeypatch.setattr(cloud, 'github_get', github_get)

    def encode(value=None):
        raw = json.dumps(packet if value is None else value, ensure_ascii=False).encode()
        return Fernet(key).encrypt(raw).decode(), hashlib.sha256(raw).hexdigest()

    return SimpleNamespace(settings=settings, packet=packet, key=key, directory=directory,
                           calls=calls, constructors=constructors, actions=actions,
                           remote_checks=remote_checks, encode=encode)


def run(thumbnail, packet=None):
    ciphertext, digest = thumbnail.encode(packet)
    if not (thumbnail.directory / 'claim.json').exists():
        manual_thumbnail.reserve_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    return manual_thumbnail.run_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)


def prepared_article_packet(thumbnail):
    result = json.loads((thumbnail.directory / 'result.json').read_text())
    return {
        'version': 'manual-request-v1', 'request_id': REQUEST_ID, 'date': DAY,
        'approval_reference': 'Sentinel_test_only_article_and_visual_approval',
        'approved_by': 'repository_owner', 'scope': 'draft_only', 'category': 'origins',
        'question': thumbnail.packet['question'], 'context': '확인한 뜻을 짧게 설명해주세요.',
        'sources': copy.deepcopy(thumbnail.packet['sources']),
        'budget': copy.deepcopy(thumbnail.packet['budget']),
        'thumbnail': {'prepared_request_id': REQUEST_ID, 'sha256': result['photo']['sha256'],
                      'approved': True, 'role': 'thumbnail', 'generated': True, 'caption': ''},
    }


def test_thumbnail_request_reserves_exactly_one_existing_model_call(thumbnail):
    before_db = thumbnail.settings.db_path.read_bytes()
    before_config = copy.deepcopy(thumbnail.settings.config)
    result = run(thumbnail)
    assert result == {'request_id': REQUEST_ID, 'status': 'MANUAL_THUMBNAIL_READY',
                      'requires_visual_review': True, 'cached': False}
    assert len(thumbnail.calls) == 1
    call = thumbnail.calls[0]
    assert {key: call[key] for key in ('model', 'quality', 'size', 'output_format', 'n')} == {
        'model': 'gpt-image-2.5-flare', 'quality': 'medium', 'size': '1024x1024',
        'output_format': 'jpeg', 'n': 1}
    assert thumbnail.packet['title'] in call['prompt']
    assert thumbnail.packet['summary'] in call['prompt']
    assert all(kwargs['max_retries'] == 0 for kwargs in thumbnail.constructors)
    assert thumbnail.settings.db_path.read_bytes() == before_db
    assert thumbnail.settings.config == before_config
    assert not list(thumbnail.settings.inbox_dir.glob('*/request.json'))
    assert not (thumbnail.settings.db_path.parent / 'ready.json').exists()


def test_generated_thumbnail_is_not_automatically_visually_approved(thumbnail):
    run(thumbnail)
    result = json.loads((thumbnail.directory / 'result.json').read_text())
    assert result['status'] == 'MANUAL_THUMBNAIL_READY'
    assert result['requires_visual_review'] is True
    assert result['photo']['generated'] is True
    assert result['photo']['role'] == 'thumbnail'
    assert result['photo'].get('approved') is not True
    assert Path(result['photo']['file']).read_bytes() == IMAGE
    assert result['photo']['sha256'] == hashlib.sha256(IMAGE).hexdigest()
    claim = json.loads((thumbnail.directory / 'claim.json').read_text())
    assert claim['cost_estimate']['not_a_hard_cost_cap'] is True
    assert float(claim['cost_estimate']['estimated_usd']) <= 3
    assert float(claim['cost_estimate']['image_scenario_usd']) > 0


def test_exact_completed_thumbnail_replays_without_new_image_call(thumbnail):
    run(thumbnail)
    original = (thumbnail.directory / 'paid-call.json').read_bytes()
    result = run(thumbnail)
    assert result['status'] == 'MANUAL_THUMBNAIL_READY' and result['cached'] is True
    assert len(thumbnail.calls) == 1
    assert (thumbnail.directory / 'paid-call.json').read_bytes() == original


@pytest.mark.parametrize('invalid', ['plaintext', 'wrong_key', 'wrong_digest', 'wrong_id',
                                   'oversize', 'path_selector'])
def test_thumbnail_envelope_or_selector_error_is_nonpaid(thumbnail, invalid):
    ciphertext, digest = thumbnail.encode()
    selector = REQUEST_ID
    if invalid == 'plaintext':
        ciphertext = json.dumps(thumbnail.packet)
    elif invalid == 'wrong_key':
        ciphertext = Fernet(Fernet.generate_key()).encrypt(b'{}').decode()
    elif invalid == 'wrong_digest':
        digest = '0' * 64
    elif invalid == 'wrong_id':
        selector = 'another-request'
    elif invalid == 'oversize':
        ciphertext = 'a' * 30001
    else:
        selector = '../escape'
    with pytest.raises((ValueError, InvalidToken)):
        manual_thumbnail.reserve_thumbnail(thumbnail.settings, ciphertext, selector, digest)
    assert thumbnail.calls == thumbnail.remote_checks == []
    assert not thumbnail.directory.exists()


@pytest.mark.parametrize('field,value', [
    ('version', 'manual-request-v1'), ('date', '2026-10-09'), ('date', '2026-10-11'),
    ('scope', 'draft_only'), ('scope', 'publish'), ('category', 'parenting'),
    ('approved_by', 'source_document'), ('approval_reference', ''),
    ('approval_reference', 'Sentinel_'), ('question', ''), ('title', ''),
    ('title', 'x' * 81), ('summary', ''), ('summary', 'x' * 601),
    ('sources', []), ('sources', [{'url': SOURCE, 'excerpt': ''}]),
    ('sources', [{'url': 'http://example.test', 'excerpt': 'source'}]),
    ('sources', [{'url': 'https://user:secret@example.test', 'excerpt': 'source'}]),
])
def test_invalid_thumbnail_brief_never_reserves_or_spends(thumbnail, field, value):
    packet = {**thumbnail.packet, field: value}
    with pytest.raises(ValueError):
        run(thumbnail, packet)
    assert thumbnail.calls == thumbnail.remote_checks == []
    assert not thumbnail.directory.exists()


@pytest.mark.parametrize('key', ['thumbnail', 'photos', 'image_model', 'publish', 'n'])
def test_thumbnail_brief_cannot_smuggle_generation_overrides(thumbnail, key):
    packet = {**thumbnail.packet, key: 2}
    with pytest.raises(ValueError):
        run(thumbnail, packet)
    assert thumbnail.calls == []
    assert not thumbnail.directory.exists()


@pytest.mark.parametrize('key,value', [('model', 'another-image-model'), ('quality', 'high'),
                                     ('size', '1536x1024'), ('output_format', 'png')])
def test_existing_image_configuration_drift_is_never_silently_overridden(thumbnail, key, value):
    thumbnail.settings.config['images'][key] = value
    with pytest.raises(ValueError, match='configuration drift'):
        run(thumbnail)
    assert thumbnail.calls == thumbnail.remote_checks == []


def test_strict_usd_prevents_even_thumbnail_reservation(thumbnail):
    packet = copy.deepcopy(thumbnail.packet)
    packet['budget']['budget_mode'] = 'strict_usd'
    with pytest.raises(CostBoundUnavailable):
        run(thumbnail, packet)
    assert thumbnail.calls == thumbnail.constructors == thumbnail.remote_checks == []
    assert not thumbnail.directory.exists()


def test_insufficient_combined_estimate_holds_before_thumbnail_call(thumbnail):
    packet = copy.deepcopy(thumbnail.packet)
    packet['budget']['max_estimated_usd'] = 2.48
    with pytest.raises(ValueError, match='estimate exceeds'):
        run(thumbnail, packet)
    assert thumbnail.calls == thumbnail.remote_checks == []


@pytest.mark.parametrize('error', ['timeout', '429', '500', 'empty', 'invalid_image'])
def test_any_failed_or_uncertain_thumbnail_is_never_paid_again(thumbnail, error):
    if error == 'timeout':
        thumbnail.actions.append(TimeoutError('Unknown image outcome'))
    elif error in {'429', '500'}:
        response = httpx.Response(int(error), request=httpx.Request('POST', 'https://offline.test'))
        thumbnail.actions.append(APIStatusError('Offline error', response=response, body={}))
    elif error == 'empty':
        thumbnail.actions.append(SimpleNamespace(data=[SimpleNamespace(b64_json='')]))
    else:
        thumbnail.actions.append(b'not a decoded image')
    with pytest.raises((ImagePending, OSError)):
        run(thumbnail)
    claim = (thumbnail.directory / 'claim.json').read_bytes()
    paid = (thumbnail.directory / 'paid-call.json').read_bytes()
    assert json.loads(claim)['status'] == 'HELD'
    assert json.loads(paid)['status'] == 'STARTED'
    for _ in range(2):
        assert run(thumbnail)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(thumbnail.calls) == 1
    assert (thumbnail.directory / 'claim.json').read_bytes() == claim
    assert (thumbnail.directory / 'paid-call.json').read_bytes() == paid


def test_global_manual_lock_also_blocks_thumbnail_submission(thumbnail):
    with (media_claim(thumbnail.settings.db_path.parent, 'manual-requests'),
          pytest.raises(MediaBusy)):
        run(thumbnail)
    assert thumbnail.calls == []


@pytest.mark.parametrize('missing', ['budget_mode', 'max_estimated_usd', 'cost_approval_reference'])
def test_thumbnail_needs_its_declared_estimated_cost_consent(thumbnail, missing):
    packet = copy.deepcopy(thumbnail.packet)
    del packet['budget'][missing]
    with pytest.raises(ValueError):
        run(thumbnail, packet)
    assert thumbnail.calls == thumbnail.remote_checks == []


def test_paid_thumbnail_requires_preceding_reservation(thumbnail):
    ciphertext, digest = thumbnail.encode()
    with pytest.raises((ValueError, FileNotFoundError)):
        manual_thumbnail.run_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert thumbnail.calls == thumbnail.constructors == []


@pytest.mark.parametrize('change', ['different_run', 'rerun'])
def test_stranded_thumbnail_reservation_cannot_be_consumed_by_rerun(thumbnail, monkeypatch, change):
    ciphertext, digest = thumbnail.encode()
    manual_thumbnail.reserve_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    if change == 'different_run':
        monkeypatch.setenv('GITHUB_RUN_ID', '456')
        assert manual_thumbnail.run_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest) == {
            'request_id': REQUEST_ID, 'status': 'MANUAL_CHECK_REQUIRED'}
    else:
        monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
        with pytest.raises(ValueError, match='first-attempt owner workflow'):
            manual_thumbnail.run_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert thumbnail.calls == []


def test_thumbnail_needs_its_own_stage_remote_reservation(thumbnail, monkeypatch):
    ciphertext, digest = thumbnail.encode()
    manual_thumbnail.reserve_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    monkeypatch.setattr(cloud, 'github_get', lambda path: json.dumps({'artifacts': [
        {'name': manual_requests.reservation_name('123', thumbnail.packet),
         'expired': False}]}).encode())
    with pytest.raises(ValueError, match='upload is not verified'):
        manual_thumbnail.run_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert thumbnail.calls == []


@pytest.mark.parametrize('identity', ['request_id', 'approval_reference', 'question'])
def test_remote_thumbnail_identity_blocks_new_id_retry(thumbnail, monkeypatch, identity):
    old = {**thumbnail.packet, 'request_id': 'other-request',
           'approval_reference': 'Sentinel_other_explicit_thumbnail_approval',
           'question': '다른 질문의 썸네일입니다.'}
    old[identity] = thumbnail.packet[identity]
    monkeypatch.setattr(cloud, 'github_get', lambda path: json.dumps({'artifacts': [
        {'name': manual_requests.reservation_name('999', old, 'thumbnail'),
         'expired': True}]}).encode())
    with pytest.raises(ValueError, match='already consumes'):
        run(thumbnail)
    assert thumbnail.calls == []
    assert not thumbnail.directory.exists()


@pytest.mark.parametrize('tampering', ['title', 'review_flag', 'photo_hash', 'image_bytes'])
def test_cached_thumbnail_integrity_fails_without_paid_repair(thumbnail, tampering):
    run(thumbnail)
    path = thumbnail.directory / 'result.json'
    result = json.loads(path.read_text())
    if tampering == 'title':
        result['title'] = '변경된 제목'
    elif tampering == 'review_flag':
        result['requires_visual_review'] = False
    elif tampering == 'photo_hash':
        result['photo']['sha256'] = '0' * 64
    else:
        Path(result['photo']['file']).write_bytes(b'changed image bytes')
    if tampering != 'image_bytes':
        path.write_text(json.dumps(result))
    with pytest.raises(ValueError):
        run(thumbnail)
    assert len(thumbnail.calls) == 1


def test_completed_thumbnail_reservation_exports_cache_without_remote_lookup(
        thumbnail, monkeypatch, tmp_path):
    run(thumbnail)
    monkeypatch.setattr(cloud, 'github_get', lambda path: pytest.fail('No cached remote read'))
    output = tmp_path / 'github-output.txt'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    ciphertext, digest = thumbnail.encode()
    result = manual_thumbnail.reserve_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert result['cached'] is True
    assert output.read_text() == 'cached=true\nartifact_name=\n'
    assert len(thumbnail.calls) == 1


def test_encrypted_thumbnail_transport_retains_nested_files_and_never_ready_post(thumbnail, tmp_path):
    run(thumbnail)
    destination = tmp_path / 'handoff' / 'bundle.enc'
    cloud.pack(thumbnail.settings, destination)
    with zipfile.ZipFile(io.BytesIO(Fernet(thumbnail.key).decrypt(destination.read_bytes()))) as archive:
        root = f'manual-thumbnails/{REQUEST_ID}/'
        for filename in ['claim.json', 'approved-packet.json', 'paid-call.json', 'result.json',
                         'image/.image-plan']:
            assert root + filename in archive.namelist()
        images = [name for name in archive.namelist() if name.startswith(root) and name.endswith('.jpg')]
        assert len(images) == 1 and archive.read(images[0]) == IMAGE
        assert json.loads(archive.read('ready.json'))['posts'] == []
    restored = tmp_path / 'restored'
    cloud.extract_bundle(destination.read_bytes(), restored, thumbnail.key.decode())
    settings = replace(thumbnail.settings, db_path=restored / 'blog.db',
                       artifact_dir=restored / 'drafts', inbox_dir=restored / 'inbox')
    ciphertext, digest = thumbnail.encode()
    assert manual_thumbnail.run_thumbnail(settings, ciphertext, REQUEST_ID, digest)['cached'] is True
    result = json.loads((restored / 'manual-thumbnails' / REQUEST_ID / 'result.json').read_text())
    assert Path(result['photo']['file']).is_relative_to(restored)
    assert len(thumbnail.calls) == 1


@pytest.mark.parametrize('filename', ['claim.json', 'paid-call.json', 'approved-packet.json'])
def test_stale_bundle_cannot_erase_or_rewind_thumbnail_spend_evidence(
        thumbnail, tmp_path, filename):
    run(thumbnail)
    destination = tmp_path / 'handoff' / 'bundle.enc'
    cloud.pack(thumbnail.settings, destination)
    altered = io.BytesIO()
    target = f'manual-thumbnails/{REQUEST_ID}/{filename}'
    with (zipfile.ZipFile(io.BytesIO(Fernet(thumbnail.key).decrypt(destination.read_bytes()))) as old,
          zipfile.ZipFile(altered, 'w') as archive):
        for name in old.namelist():
            if name != target:
                archive.writestr(name, old.read(name))
    before = {p.relative_to(thumbnail.settings.db_path.parent): p.read_bytes()
              for p in thumbnail.settings.db_path.parent.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='checkpoint conflict'):
        cloud.extract_bundle(Fernet(thumbnail.key).encrypt(altered.getvalue()),
                             thumbnail.settings.db_path.parent, thumbnail.key.decode())
    after = {p.relative_to(thumbnail.settings.db_path.parent): p.read_bytes()
             for p in thumbnail.settings.db_path.parent.rglob('*') if p.is_file()}
    assert after == before
    assert len(thumbnail.calls) == 1


def test_cancelled_image_runner_cannot_charge_again_from_pre_spend_artifact(
        thumbnail, monkeypatch, tmp_path):
    ciphertext, digest = thumbnail.encode()
    manual_thumbnail.reserve_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    remote = tmp_path / 'reservation' / 'bundle.enc'
    cloud.pack(thumbnail.settings, remote)
    thumbnail.actions.append(TimeoutError('Runner lost after submission'))
    with pytest.raises(ImagePending):
        manual_thumbnail.run_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert len(thumbnail.calls) == 1
    fresh = tmp_path / 'fresh-runner'
    cloud.extract_bundle(remote.read_bytes(), fresh, thumbnail.key.decode())
    settings = replace(thumbnail.settings, db_path=fresh / 'blog.db',
                       artifact_dir=fresh / 'drafts', inbox_dir=fresh / 'inbox')
    monkeypatch.setenv('GITHUB_RUN_ID', '456')
    assert manual_thumbnail.run_thumbnail(settings, ciphertext, REQUEST_ID, digest) == {
        'request_id': REQUEST_ID, 'status': 'MANUAL_CHECK_REQUIRED'}
    assert len(thumbnail.calls) == 1


@pytest.mark.parametrize('failure_point,expected_calls', [('claim', 0), ('paid_call', 0),
                                                       ('result', 1)])
def test_thumbnail_checkpoint_failure_never_unlocks_paid_retry(
        thumbnail, monkeypatch, failure_point, expected_calls):
    ciphertext, digest = thumbnail.encode()
    manual_thumbnail.reserve_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    original = manual_thumbnail.atomic_json

    def write(path, value):
        fail = ((failure_point == 'claim' and path.name == 'claim.json'
                 and value['status'] == 'STARTED')
                or (failure_point == 'paid_call' and path.name == 'paid-call.json')
                or (failure_point == 'result' and path.name == 'result.json'))
        if fail:
            raise OSError('Offline checkpoint failure')
        original(path, value)

    monkeypatch.setattr(manual_thumbnail, 'atomic_json', write)
    with pytest.raises((ImagePending, OSError)):
        manual_thumbnail.run_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert len(thumbnail.calls) == expected_calls
    if failure_point != 'claim':
        assert run(thumbnail)['status'] == 'MANUAL_CHECK_REQUIRED'
        assert len(thumbnail.calls) == expected_calls


@pytest.mark.parametrize('mode,expected_status,expected_calls', [
    ('manual-thumbnail-reserve', 'MANUAL_THUMBNAIL_RESERVED', 0),
    ('manual-thumbnail', 'MANUAL_THUMBNAIL_READY', 1),
])
def test_cloud_thumbnail_mode_skips_all_text_daily_and_saving_stages(
        thumbnail, monkeypatch, tmp_path, capsys, mode, expected_status, expected_calls):
    token, digest = thumbnail.encode()
    if mode == 'manual-thumbnail':
        manual_thumbnail.reserve_thumbnail(thumbnail.settings, token, REQUEST_ID, digest)
    monkeypatch.setattr(cloud, 'load_settings', lambda: thumbnail.settings)
    monkeypatch.setattr(cloud, 'restore', lambda directory: None)
    for function in ['seed_inputs', 'run_daily', 'recover_preparation', 'import_manual_saves']:
        monkeypatch.setattr(cloud, function, lambda *a, **kw: pytest.fail('No automatic pipeline'))
    monkeypatch.setenv('BLOG_MANUAL_REQUEST_PACKET', token)
    output = tmp_path / 'handoff' / 'bundle.enc'
    monkeypatch.setenv('BLOG_ENCRYPTED_OUTPUT', str(output))
    monkeypatch.setattr('sys.argv', ['cloud', mode, '--manual-request-id', REQUEST_ID,
                                    '--manual-request-sha256', digest])
    cloud.main()
    assert output.exists()
    assert len(thumbnail.calls) == expected_calls
    stdout = capsys.readouterr().out
    assert expected_status in stdout
    assert token not in stdout
    assert thumbnail.packet['title'] not in stdout
    assert thumbnail.packet['approval_reference'] not in stdout


def test_workflow_uses_same_required_reservation_upload_for_thumbnail_mode():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/blog-prepare.yml').read_text()
    reservation = workflow.index('name: Reserve explicitly approved manual request')
    upload = workflow.index('name: Persist manual reservation before paid work')
    paid = workflow.index('name: Prepare or resume')
    assert reservation < upload < paid
    assert "inputs.mode == 'manual-thumbnail'" in workflow[reservation:upload]
    assert 'manual-thumbnail-reserve' in workflow[reservation:upload]
    assert "inputs.mode == 'manual-thumbnail'" in workflow[upload:paid]
    assert "steps.manual_reservation.outputs.cached != 'true'" in workflow[upload:paid]
    assert "inputs.mode != 'manual-thumbnail'" in workflow[paid:]
    assert "steps.manual_reservation.outputs.cached != 'true'" in workflow[paid:]


def test_reported_image_usage_is_recorded_for_later_text_budget(thumbnail):
    usage = {'input_tokens': 1000, 'output_tokens': 439}
    thumbnail.actions.append(SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(IMAGE).decode())],
        usage=SimpleNamespace(model_dump=lambda: usage), _request_id='offline-image-usage'))
    run(thumbnail)
    result = json.loads((thumbnail.directory / 'result.json').read_text())
    assert result['cost_basis'] == 'reported_usage'
    assert Decimal(result['accounted_cost_usd']) == Decimal('0.01817')
    assert result['completed_date_kst'] == DAY
    paid = json.loads((thumbnail.directory / 'paid-call.json').read_text())
    assert paid['actual_call_date_kst'] == DAY


def test_inline_image_cannot_erase_existing_prepared_project_cost(thumbnail):
    run(thumbnail)
    packet = prepared_article_packet(thumbnail)
    packet['thumbnail'] = {'data_base64': base64.b64encode(IMAGE).decode(),
                           'extension': 'jpg', 'sha256': hashlib.sha256(IMAGE).hexdigest(),
                           'approved': True, 'generated': True, 'role': 'thumbnail', 'caption': ''}
    ciphertext, digest = thumbnail.encode(packet)
    with pytest.raises(ValueError, match='exact approved reference'):
        manual_requests.reserve_manual_request(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert len(thumbnail.calls) == 1
    assert not (thumbnail.settings.db_path.parent / 'manual-requests' / REQUEST_ID).exists()


@pytest.mark.parametrize('invalid', ['question', 'sources', 'budget', 'hash', 'approval',
                                   'prepared_id', 'generated'])
def test_prepared_thumbnail_reference_binds_exact_project_and_visual_approval(thumbnail, invalid):
    run(thumbnail)
    packet = prepared_article_packet(thumbnail)
    if invalid == 'question':
        packet['question'] = '다른 이름은 무슨 뜻인가요?'
    elif invalid == 'sources':
        packet['sources'][0]['excerpt'] = '승인하지 않은 다른 근거입니다.'
    elif invalid == 'budget':
        packet['budget']['max_estimated_usd'] = 2.99
    elif invalid == 'hash':
        packet['thumbnail']['sha256'] = '0' * 64
    elif invalid == 'approval':
        packet['thumbnail']['approved'] = False
    elif invalid == 'prepared_id':
        packet['thumbnail']['prepared_request_id'] = 'other-request'
    else:
        packet['thumbnail']['generated'] = False
    ciphertext, digest = thumbnail.encode(packet)
    with pytest.raises(ValueError):
        manual_requests.validate_packet(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert len(thumbnail.calls) == 1
    assert not (thumbnail.settings.db_path.parent / 'manual-requests' / REQUEST_ID).exists()


def test_prior_day_thumbnail_keeps_original_date_when_current_text_is_approved(
        thumbnail, monkeypatch):
    run(thumbnail)
    original = (thumbnail.directory / 'claim.json').read_bytes()
    packet = prepared_article_packet(thumbnail)
    packet['date'] = '2026-10-11'
    monkeypatch.setattr(manual_requests, 'today_kst', lambda: date(2026, 10, 11))
    ciphertext, digest = thumbnail.encode(packet)
    assert manual_requests.validate_packet(thumbnail.settings, ciphertext, REQUEST_ID, digest) == packet
    assert (thumbnail.directory / 'claim.json').read_bytes() == original
    assert json.loads(original)['date'] == DAY
    assert len(thumbnail.calls) == 1


def test_approved_prepared_thumbnail_flows_into_text_without_a_second_image_call(
        thumbnail, monkeypatch):
    run(thumbnail)
    packet = prepared_article_packet(thumbnail)
    text_calls = []

    def create(**kwargs):
        text_calls.append(kwargs)
        if kwargs['text']['format']['name'] == 'reviewer':
            payload = {'scores': [5] * 6, 'total': 30, 'decision': 'PASS',
                       'issues': [], 'blocking_issues': [], 'rewrite_instructions': '',
                       'source_checks': [{'claim': '공기의 뜻', 'source_url': SOURCE,
                                          'evidence': '사전에서 확인한 그릇의 뜻',
                                          'status': 'SUPPORTED'}]}
        else:
            payload = {'title': thumbnail.packet['title'], 'subcategory': '음식·생활',
                       'body': thumbnail.packet['summary'], 'tags': ['공깃밥'],
                       'source_urls': [SOURCE], 'as_of_date': DAY}
        return SimpleNamespace(
            id=f'offline-text-{len(text_calls)}', model='gpt-5', status='completed',
            output_text=json.dumps(payload), usage=None,
            output=[{'type': 'web_search_call', 'status': 'completed',
                     'action': {'type': 'open_page', 'url': SOURCE}}])

    monkeypatch.setattr('blogbot.llm.OpenAI',
                        lambda **kw: SimpleNamespace(responses=SimpleNamespace(create=create)))
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)

    def github_get(path):
        if path.startswith('/actions/artifacts?'):
            return b'{"artifacts": []}'
        return json.dumps({'artifacts': [
            {'name': manual_requests.reservation_name('123', packet), 'expired': False}]}).encode()

    monkeypatch.setattr(cloud, 'github_get', github_get)
    ciphertext, digest = thumbnail.encode(packet)
    before_db = thumbnail.settings.db_path.read_bytes()
    manual_requests.reserve_manual_request(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    result = manual_requests.run_manual_request(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert result['status'] == 'MANUAL_DRAFT_READY'
    assert len(text_calls) == 2 and len(thumbnail.calls) == 1
    assert all(call['service_tier'] == 'default' for call in text_calls)
    directory = thumbnail.settings.db_path.parent / 'manual-requests' / REQUEST_ID
    draft = json.loads((directory / 'result.json').read_text())
    photo = draft['post']['photos'][0]
    assert photo['approved'] is True and photo['sha256'] == hashlib.sha256(IMAGE).hexdigest()
    assert Path(photo['file']).read_bytes() == IMAGE
    assert thumbnail.settings.db_path.read_bytes() == before_db


def test_midnight_before_image_submission_holds_without_a_charge(thumbnail, monkeypatch):
    ciphertext, digest = thumbnail.encode()
    manual_thumbnail.reserve_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    original = manual_thumbnail.execute_image_plans

    def cross_midnight(*args, **kwargs):
        monkeypatch.setattr(manual_thumbnail, 'today_kst', lambda: date(2026, 10, 11))
        return original(*args, **kwargs)

    monkeypatch.setattr(manual_thumbnail, 'execute_image_plans', cross_midnight)
    with pytest.raises(ImagePending):
        manual_thumbnail.run_thumbnail(thumbnail.settings, ciphertext, REQUEST_ID, digest)
    assert thumbnail.calls == []
    assert not (thumbnail.directory / 'paid-call.json').exists()
    assert json.loads((thumbnail.directory / 'claim.json').read_text())['status'] == 'HELD'
