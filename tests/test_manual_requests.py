"""Offline contracts for one explicitly approved, additional draft-only request."""
import base64
import copy
import hashlib
import io
import json
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image

from blogbot import cloud, manual_requests
from blogbot.config import load_settings
from blogbot.core import PostDraft, connect_db, import_work_receipts, save_post
from blogbot.planning import MediaBusy, media_claim, saved_count

DAY = '2026-10-10'
REQUEST_ID = 'owner-origins-additional-20261010'
SOURCE = 'https://stdict.korean.go.kr/search/searchView.do?word_no=123'
image_bytes = io.BytesIO()
Image.new('RGB', (24, 24), '#4c82ae').save(image_bytes, format='PNG')
IMAGE = image_bytes.getvalue()


def draft_payload(**overrides):
    return {
        'title': '공깃밥의 공기는 어떤 뜻일까요?', 'subcategory': '음식·생활',
        'body': '**공기**는 밥을 담아 먹는 작은 그릇을 뜻합니다.\n\n'
                '공깃밥은 그 그릇에 담은 밥을 가리킵니다. 사전의 현재 뜻과 '
                '확인되지 않은 역사적 유래는 구분해서 설명합니다.',
        'tags': ['공깃밥', '이름의유래'], 'source_urls': [SOURCE],
        'as_of_date': DAY, **overrides,
    }


def review_payload(**overrides):
    return {
        'scores': [5] * 6, 'total': 30, 'decision': 'PASS', 'issues': [],
        'blocking_issues': [], 'rewrite_instructions': '',
        'source_checks': [{'claim': '공기의 뜻', 'source_url': SOURCE,
                           'evidence': '사전에서 확인한 그릇의 뜻', 'status': 'SUPPORTED'}],
        **overrides,
    }


@pytest.fixture
def manual(tmp_path, monkeypatch):
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
    packet = {
        'version': 'manual-request-v1', 'request_id': REQUEST_ID, 'date': DAY,
        'approval_reference': 'Sentinel_test_explicit_owner_approval',
        'approved_by': 'repository_owner', 'scope': 'draft_only', 'category': 'origins',
        'question': '공깃밥의 공기는 무슨 뜻인가요?',
        'context': '이름의 뜻과 역사적 유래를 구분해 짧게 설명해주세요.',
        'sources': [{'url': SOURCE, 'excerpt': '공기는 밥을 담아 먹는 작은 그릇입니다.'}],
        'thumbnail': {'data_base64': base64.b64encode(IMAGE).decode(),
                      'sha256': hashlib.sha256(IMAGE).hexdigest(), 'extension': 'png',
                      'approved': True, 'role': 'thumbnail', 'generated': True, 'caption': ''},
        'budget': {'writer_model': settings.openai_model,
                   'review_model': settings.review_model, 'max_calls': 4,
                   'max_output_tokens': 36000,
                   'budget_mode': 'estimated_with_call_caps', 'max_estimated_usd': 3,
                   'cost_approval_reference': 'Sentinel_test_estimated_cost_approval'},
    }
    calls, constructors, actions = [], [], []

    def create(**kwargs):
        calls.append(kwargs)
        stage = kwargs['text']['format']['name']
        action = actions.pop(0) if actions else None
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, SimpleNamespace):
            return action
        payload = action if action is not None else (
            review_payload() if stage == 'reviewer' else draft_payload())
        return SimpleNamespace(
            id=f'offline-response-{len(calls)}', model=kwargs['model'], status='completed',
            output_text=json.dumps(payload, ensure_ascii=False),
            output=[{'type': 'web_search_call', 'status': 'completed',
                     'action': {'type': 'open_page', 'url': SOURCE}}], usage=None,
        )

    def make_client(**kwargs):
        constructors.append(kwargs)
        return SimpleNamespace(responses=SimpleNamespace(create=create))

    monkeypatch.setattr('blogbot.llm.OpenAI', make_client)
    monkeypatch.setattr('blogbot.images.OpenAI',
                        lambda **kwargs: pytest.fail('Manual requests cannot generate images'))
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)
    monkeypatch.setattr('blogbot.pipeline.make_writer',
                        lambda *args: pytest.fail('Manual requests cannot write to Naver'))
    remote_checks, history_checks = [], []

    def github_get(path):
        if path.startswith('/actions/artifacts?'):
            assert path == '/actions/artifacts?per_page=100&page=1'
            history_checks.append(path)
            return b'{"artifacts": []}'
        assert path == '/actions/runs/123/artifacts?per_page=100'
        remote_checks.append(path)
        return json.dumps({'artifacts': [
            {'name': manual_requests.reservation_name('123', packet), 'expired': False}]}).encode()

    monkeypatch.setattr(cloud, 'github_get', github_get)

    def encode(value=None):
        raw = json.dumps(packet if value is None else value, ensure_ascii=False).encode()
        return Fernet(key).encrypt(raw).decode(), hashlib.sha256(raw).hexdigest()

    return SimpleNamespace(settings=settings, packet=packet, key=key, calls=calls,
                           constructors=constructors, actions=actions, encode=encode,
                           remote_checks=remote_checks, history_checks=history_checks,
                           directory=settings.db_path.parent / 'manual-requests' / REQUEST_ID)


def run(manual, packet=None):
    ciphertext, digest = manual.encode(packet)
    if not (manual.directory / 'claim.json').exists():
        manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    return manual_requests.run_manual_request(
        manual.settings, ciphertext, REQUEST_ID, digest)


def test_authenticated_packet_matches_selector_and_preserves_approval(manual):
    ciphertext, digest = manual.encode()
    validated = manual_requests.validate_packet(manual.settings, ciphertext, REQUEST_ID, digest)
    assert validated['request_id'] == REQUEST_ID
    assert validated['approval_reference'] == manual.packet['approval_reference']
    assert validated['scope'] == 'draft_only'
    assert manual.calls == []


@pytest.mark.parametrize('invalid', [
    'plaintext', 'wrong_key', 'tampered_ciphertext', 'changed_plaintext', 'oversized_ciphertext',
    'wrong_selector', 'path_selector', 'missing_digest', 'invalid_digest', 'uppercase_digest',
])
def test_invalid_envelope_never_claims_or_calls(manual, invalid):
    ciphertext, digest = manual.encode()
    selector = REQUEST_ID
    if invalid == 'plaintext':
        ciphertext = json.dumps(manual.packet)
    elif invalid == 'wrong_key':
        ciphertext = Fernet(Fernet.generate_key()).encrypt(b'{}').decode()
    elif invalid == 'tampered_ciphertext':
        ciphertext = ciphertext[:30] + ('A' if ciphertext[30] != 'A' else 'B') + ciphertext[31:]
    elif invalid == 'changed_plaintext':
        ciphertext, _ = manual.encode({**manual.packet, 'question': '다른 질문입니다.'})
    elif invalid == 'oversized_ciphertext':
        ciphertext = 'a' * 60001
    elif invalid == 'wrong_selector':
        selector = 'other-request'
    elif invalid == 'path_selector':
        selector = '../escape'
    elif invalid == 'missing_digest':
        digest = ''
    elif invalid == 'invalid_digest':
        digest = 'g' * 64
    else:
        digest = digest.upper()
    with pytest.raises((ValueError, InvalidToken)):
        manual_requests.run_manual_request(manual.settings, ciphertext, selector, digest)
    assert manual.calls == []
    assert not manual.directory.exists()


@pytest.mark.parametrize('path,value', [
    (('version',), 'manual-request-v2'), (('request_id',), 'different-request'),
    (('date',), '2026-10-09'), (('date',), '2026-10-11'), (('date',), '20261010'),
    (('category',), 'parenting'), (('scope',), 'publish'), (('scope',), 'save_draft'),
    (('approved_by',), 'source_document'), (('approval_reference',), ''),
    (('approval_reference',), 'Sentinel_'), (('approval_reference',), 'someone_said_yes'),
    (('approval_reference',), 'Sentinel_ approval'), (('question',), ''),
    (('question',), None), (('context',), 7), (('sources',), []),
    (('sources',), [{'url': SOURCE, 'excerpt': ''}]),
    (('sources',), [{'url': 'http://example.test/source', 'excerpt': 'source'}]),
    (('sources',), [{'url': 'https://user:secret@example.test/source', 'excerpt': 'source'}]),
    (('sources',), [{'url': 'https://example.test/source', 'excerpt': 'source',
                    'owner_approved': True}]),
    (('thumbnail',), []), (('thumbnail', 'approved'), False),
    (('thumbnail', 'approved'), 1), (('thumbnail', 'generated'), 'true'),
    (('thumbnail', 'generated'), 1), (('thumbnail', 'role'), 'body'),
    (('thumbnail', 'extension'), '../png'), (('thumbnail', 'extension'), 'svg'),
    (('thumbnail', 'data_base64'), '%%%'), (('thumbnail', 'data_base64'), ''),
    (('thumbnail', 'data_base64'), base64.b64encode(b'different image').decode()),
    (('thumbnail', 'sha256'), '0' * 64), (('thumbnail', 'caption'), None),
    (('budget', 'writer_model'), 'unapproved-model'),
    (('budget', 'review_model'), 'unapproved-model'),
    (('budget', 'max_calls'), 1), (('budget', 'max_calls'), 5),
    (('budget', 'max_calls'), True), (('budget', 'max_calls'), 2.0),
    (('budget', 'max_output_tokens'), 17999), (('budget', 'max_output_tokens'), 36001),
    (('budget', 'max_output_tokens'), True), (('budget', 'max_output_tokens'), 18000.0),
    (('budget', 'budget_mode'), ''), (('budget', 'budget_mode'), 'unlimited'),
    (('budget', 'budget_mode'), 'estimated'),
    (('budget', 'max_estimated_usd'), 0), (('budget', 'max_estimated_usd'), -1),
    (('budget', 'max_estimated_usd'), 3.01), (('budget', 'max_estimated_usd'), True),
    (('budget', 'max_estimated_usd'), '3'),
    (('budget', 'max_estimated_usd'), float('inf')),
    (('budget', 'max_estimated_usd'), float('nan')),
    (('budget', 'cost_approval_reference'), ''),
    (('budget', 'cost_approval_reference'), 'Sentinel_'),
    (('budget', 'cost_approval_reference'), 'source_page_says_approved'),
    (('budget', 'cost_approval_reference'), None),
])
def test_unapproved_or_malformed_packet_is_rejected_before_paid_work(manual, path, value):
    packet = copy.deepcopy(manual.packet)
    target = packet
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        run(manual, packet)
    assert manual.calls == []
    assert not manual.directory.exists()


@pytest.mark.parametrize('path', [(), ('thumbnail',), ('budget',)])
def test_packet_allowlists_reject_undeclared_approval_or_execution_fields(manual, path):
    packet = copy.deepcopy(manual.packet)
    target = packet
    for key in path:
        target = target[key]
    target['publish'] = True
    with pytest.raises(ValueError):
        run(manual, packet)
    assert manual.calls == []


@pytest.mark.parametrize('size', [0, 30001])
def test_thumbnail_size_is_validated_even_with_matching_checksum(manual, size):
    packet = copy.deepcopy(manual.packet)
    raw = b'x' * size
    packet['thumbnail'].update(data_base64=base64.b64encode(raw).decode(),
                               sha256=hashlib.sha256(raw).hexdigest())
    with pytest.raises(ValueError):
        run(manual, packet)
    assert manual.calls == []


def test_success_is_draft_only_with_no_daily_plan_or_database_changes(manual):
    before_db = manual.settings.db_path.read_bytes()
    before_config = copy.deepcopy(manual.settings.config)
    assert before_config['daily_plan']['category'] != 'origins'
    result = run(manual)
    assert result['status'] == 'MANUAL_DRAFT_READY'
    assert result['request_id'] == REQUEST_ID
    assert manual.settings.db_path.read_bytes() == before_db
    assert manual.settings.config == before_config
    assert len(manual.calls) == 2
    assert [c['text']['format']['name'] for c in manual.calls] == ['writer', 'reviewer']
    assert sum(c['max_output_tokens'] for c in manual.calls) == 18000
    assert manual.remote_checks == ['/actions/runs/123/artifacts?per_page=100']
    assert all(c['model'] == 'gpt-5' for c in manual.calls)
    assert all(c['max_retries'] == 0 for c in manual.constructors)
    assert not list(manual.settings.inbox_dir.glob('*/request.json'))
    assert not (manual.settings.db_path.parent / 'ready.json').exists()


def test_completed_exact_packet_reuses_result_without_paid_retry(manual):
    first = run(manual)
    claim = (manual.directory / 'claim.json').read_bytes()
    second = run(manual)
    assert second['request_id'] == first['request_id']
    assert second['status'] == first['status'] == 'MANUAL_DRAFT_READY'
    assert first['cached'] is False and second['cached'] is True
    assert len(manual.calls) == 2
    assert (manual.directory / 'claim.json').read_bytes() == claim


@pytest.mark.parametrize('table', ['posts', 'attempts', 'save_receipts'])
def test_existing_request_id_in_any_ledger_blocks_before_calls(manual, table):
    with closing(connect_db(manual.settings.db_path)) as conn, conn:
        if table == 'posts':
            save_post(conn, PostDraft('origins', '음식·생활', '기존 원고', '기존 원고',
                                     '기존 내용', [], [SOURCE], '2026-10-09',
                                     request_id=REQUEST_ID))
        else:
            conn.execute(f'INSERT INTO {table}(request_id,day,category) VALUES(?,?,?)',
                         (REQUEST_ID, '2026-10-09', 'parenting'))
    before = manual.settings.db_path.read_bytes()
    with pytest.raises(ValueError):
        run(manual)
    assert manual.calls == []
    assert manual.settings.db_path.read_bytes() == before


def test_changed_packet_cannot_reuse_an_existing_request_claim(manual):
    run(manual)
    original = (manual.directory / 'claim.json').read_bytes()
    changed = {**manual.packet, 'question': '변경된 질문도 자동 승인할 수 있나요?'}
    with pytest.raises(ValueError):
        run(manual, changed)
    assert len(manual.calls) == 2
    assert (manual.directory / 'claim.json').read_bytes() == original


def test_encrypted_transport_preserves_manual_output_without_ready_post(manual, tmp_path):
    run(manual)
    destination = tmp_path / 'handoff' / 'bundle.enc'
    cloud.pack(manual.settings, destination)
    archive_bytes = Fernet(manual.key).decrypt(destination.read_bytes())
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        root = f'manual-requests/{REQUEST_ID}/'
        assert root + 'claim.json' in archive.namelist()
        assert root + 'paid-calls.json' in archive.namelist()
        assert root + 'result.json' in archive.namelist()
        assert json.loads(archive.read('ready.json'))['posts'] == []
        assert any(archive.read(name) == IMAGE for name in archive.namelist()
                   if name.startswith(root) and name.endswith('.png'))


def test_result_retains_exact_approved_thumbnail_and_private_draft_status(manual):
    run(manual)
    result = json.loads((manual.directory / 'result.json').read_text())
    assert result['post']['status'] == 'MANUAL_DRAFT_READY'
    assert result['post']['category'] == 'origins'
    assert result['post']['request_id'] == REQUEST_ID
    assert result['post']['as_of_date'] == DAY
    assert result['post']['quality_score'] == 30
    assert 'daily_plan' not in result['post']['provenance']
    assert len(result['post']['photos']) == 1
    photo = result['post']['photos'][0]
    assert photo['approved'] is True and photo['generated'] is True
    assert photo['role'] == 'thumbnail'
    assert photo['sha256'] == manual.packet['thumbnail']['sha256']
    assert Path(photo['file']).read_bytes() == IMAGE
    assert result['input']['photos'] == result['post']['photos']
    assert result['approval']['approval_reference'] == manual.packet['approval_reference']
    assert result['approval']['scope'] == 'draft_only'


@pytest.mark.parametrize('failing_stage', ['writer', 'reviewer'])
def test_failed_or_uncertain_call_is_reserved_and_never_retried(manual, failing_stage):
    if failing_stage == 'reviewer':
        manual.actions.append(draft_payload())
    manual.actions.append(TimeoutError('Uncertain API outcome'))
    with pytest.raises(TimeoutError):
        run(manual)
    calls = len(manual.calls)
    claim_bytes = (manual.directory / 'claim.json').read_bytes()
    journal_bytes = (manual.directory / 'paid-calls.json').read_bytes()
    assert json.loads(claim_bytes)['status'] == 'HELD'
    assert json.loads(journal_bytes)[-1]['status'] == 'STARTED'
    for _ in range(2):
        assert run(manual)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(manual.calls) == calls
    assert (manual.directory / 'claim.json').read_bytes() == claim_bytes
    assert (manual.directory / 'paid-calls.json').read_bytes() == journal_bytes


@pytest.mark.parametrize('state', ['STARTED', 'HELD', 'FAILED', 'UNCERTAIN', 'UNKNOWN', None])
def test_every_nonterminal_existing_claim_refuses_paid_resubmission(manual, state):
    manual.actions.append(TimeoutError('Uncertain API outcome'))
    with pytest.raises(TimeoutError):
        run(manual)
    path = manual.directory / 'claim.json'
    claim = json.loads(path.read_text())
    claim['status'] = state
    path.write_text(json.dumps(claim))
    original = path.read_bytes()
    assert run(manual)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(manual.calls) == 1
    assert path.read_bytes() == original


def test_call_claim_and_thumbnail_are_durable_before_submission(manual, monkeypatch):
    original = manual_requests.BudgetedResponses.create
    observations = []

    class Transport:
        def create(self, **kwargs):
            claim = json.loads((manual.directory / 'claim.json').read_text())
            calls = json.loads((manual.directory / 'paid-calls.json').read_text())
            assert claim['status'] == 'STARTED'
            assert calls[-1]['status'] == 'STARTED'
            assert calls[-1]['max_output_tokens'] == kwargs['max_output_tokens']
            assert (manual.directory / 'thumbnail.png').read_bytes() == IMAGE
            observations.append(calls[-1])
            raise TimeoutError('Submission outcome unknown')

    def submit_with_observer(self, **kwargs):
        self.responses = Transport()
        return original(self, **kwargs)

    monkeypatch.setattr(manual_requests.BudgetedResponses, 'create', submit_with_observer)
    with pytest.raises(TimeoutError):
        run(manual)
    assert len(observations) == 1
    assert run(manual)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(observations) == 1


def test_one_review_rewrite_fits_exact_four_call_36000_token_ceiling(manual):
    manual.actions.extend([
        draft_payload(),
        review_payload(decision='REWRITE', scores=[4] * 6, total=24,
                       issues=['설명을 정리해주세요.'], rewrite_instructions='같은 근거로 정리'),
        draft_payload(), review_payload(),
    ])
    assert run(manual)['status'] == 'MANUAL_DRAFT_READY'
    assert [c['text']['format']['name'] for c in manual.calls] == [
        'writer', 'reviewer', 'rewrite', 'reviewer']
    assert [c['max_output_tokens'] for c in manual.calls] == [12000, 6000, 12000, 6000]
    assert sum(c['max_output_tokens'] for c in manual.calls) == 36000
    assert len(json.loads((manual.directory / 'paid-calls.json').read_text())) == 4
    assert run(manual)['status'] == 'MANUAL_DRAFT_READY'
    assert len(manual.calls) == 4


@pytest.mark.parametrize('max_calls,tokens,expected_calls', [(2, 36000, 2), (4, 18000, 2),
                                                         (4, 34000, 3)])
def test_exhausted_budget_holds_before_next_submission_and_stays_held(
        manual, max_calls, tokens, expected_calls):
    packet = copy.deepcopy(manual.packet)
    packet['budget'].update(max_calls=max_calls, max_output_tokens=tokens)
    manual.actions.extend([
        draft_payload(),
        review_payload(decision='REWRITE', scores=[4] * 6, total=24,
                       issues=['수정 요청'], rewrite_instructions='같은 근거로 정리'),
        draft_payload(), review_payload(),
    ])
    with pytest.raises(ValueError, match='budget'):
        run(manual, packet)
    assert len(manual.calls) == expected_calls
    assert sum(c['max_output_tokens'] for c in manual.calls) <= tokens
    assert run(manual, packet)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(manual.calls) == expected_calls


def test_paid_adapter_reloads_persisted_reservations_after_restart(manual):
    path = manual.settings.db_path.parent / 'paid-test.json'
    submitted = []
    transport = SimpleNamespace(create=lambda **kw: submitted.append(kw))
    budget = {**manual.packet['budget'], 'max_calls': 2, 'max_output_tokens': 18000}
    manual_requests.BudgetedResponses(transport, path, budget).create(
        model='gpt-5', max_output_tokens=12000)
    restarted = manual_requests.BudgetedResponses(transport, path, budget)
    restarted.create(model='gpt-5', max_output_tokens=6000)
    with pytest.raises(ValueError, match='budget'):
        restarted.create(model='gpt-5', max_output_tokens=1)
    assert len(submitted) == 2
    assert len(json.loads(path.read_text())) == 2


@pytest.mark.parametrize('kwargs', [
    {'model': 'other-model', 'max_output_tokens': 12000},
    {'model': 'gpt-5', 'max_output_tokens': 0},
    {'model': 'gpt-5', 'max_output_tokens': -1},
    {'model': 'gpt-5', 'max_output_tokens': True},
    {'model': 'gpt-5', 'max_output_tokens': 12000.0},
    {'model': 'gpt-5', 'max_output_tokens': 36001},
])
def test_paid_adapter_rejects_model_or_token_drift_without_submission(manual, kwargs):
    path = manual.settings.db_path.parent / 'paid-test.json'
    transport = SimpleNamespace(create=lambda **kw: pytest.fail('Unapproved paid submission'))
    adapter = manual_requests.BudgetedResponses(transport, path, manual.packet['budget'])
    with pytest.raises(ValueError):
        adapter.create(**kwargs)
    assert not path.exists()


def test_restore_rebases_private_thumbnail_and_reuses_success_without_paid_calls(manual, tmp_path):
    run(manual)
    destination = tmp_path / 'handoff' / 'bundle.enc'
    cloud.pack(manual.settings, destination)
    restored = tmp_path / 'restored'
    cloud.extract_bundle(destination.read_bytes(), restored, manual.key.decode())
    settings = replace(manual.settings, db_path=restored / 'blog.db',
                       artifact_dir=restored / 'drafts', inbox_dir=restored / 'inbox')
    ciphertext, digest = manual.encode()
    assert manual_requests.run_manual_request(settings, ciphertext, REQUEST_ID, digest) == {
        'request_id': REQUEST_ID, 'status': 'MANUAL_DRAFT_READY', 'cached': True}
    result = json.loads((restored / 'manual-requests' / REQUEST_ID / 'result.json').read_text())
    photo = Path(result['post']['photos'][0]['file'])
    assert photo.is_relative_to(restored)
    assert photo.read_bytes() == IMAGE
    assert len(manual.calls) == 2


def test_global_claim_prevents_concurrent_manual_paid_runs(manual):
    with (media_claim(manual.settings.db_path.parent, 'manual-requests'),
          pytest.raises(MediaBusy)):
        run(manual)
    assert manual.calls == []
    assert not manual.directory.exists()


@pytest.mark.parametrize('duplicate', ['approval', 'question', 'normalized_question'])
@pytest.mark.parametrize('first_outcome', ['ready', 'uncertain'])
def test_new_id_cannot_reuse_approval_or_question_after_any_paid_outcome(
        manual, duplicate, first_outcome):
    if first_outcome == 'uncertain':
        manual.actions.append(TimeoutError('Unknown outcome'))
        with pytest.raises(TimeoutError):
            run(manual)
    else:
        run(manual)
    before = len(manual.calls)
    packet = copy.deepcopy(manual.packet)
    packet['request_id'] = 'another-owner-request'
    if duplicate == 'approval':
        packet['question'] = '무지개 이름은 어디에서 왔나요?'
    else:
        packet['approval_reference'] = 'Sentinel_a_different_owner_reference'
        if duplicate == 'normalized_question':
            packet['question'] = '  ' + packet['question'].replace(' ', '\n  ') + '\n'
    ciphertext, digest = manual.encode(packet)
    with pytest.raises(ValueError, match='approval or question'):
        manual_requests.reserve_manual_request(
            manual.settings, ciphertext, packet['request_id'], digest)
    assert len(manual.calls) == before
    assert not (manual.directory.parent / packet['request_id']).exists()


@pytest.mark.parametrize('status', ['SAVING', 'SAVE_UNCERTAIN', 'PUBLISHING', 'PUBLISH_UNCERTAIN'])
def test_unresolved_other_post_blocks_extra_work_without_altering_ledger(manual, status):
    with closing(connect_db(manual.settings.db_path)) as conn:
        save_post(conn, PostDraft('origins', '음식·생활', '기존 원고', '기존 원고', '기존 내용',
                                 [], [SOURCE], DAY, status=status, request_id='other-request'))
    before = manual.settings.db_path.read_bytes()
    with pytest.raises(ValueError, match='Unresolved'):
        run(manual)
    assert manual.calls == []
    assert manual.settings.db_path.read_bytes() == before


def test_review_rejection_cannot_be_promoted_by_ready_export_or_replay(manual, tmp_path):
    manual.actions.extend([draft_payload(), review_payload(scores=[2] * 6, total=12,
                                                          decision='DROP')])
    with pytest.raises(ValueError, match='review'):
        run(manual)
    assert run(manual)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(manual.calls) == 2
    assert not (manual.directory / 'result.json').exists()
    cloud.pack(manual.settings, tmp_path / 'handoff' / 'bundle.enc')
    assert json.loads((manual.settings.db_path.parent / 'ready.json').read_text())['posts'] == []


def test_pre_review_correction_consumes_shared_budget_and_cannot_buy_rewrite(manual):
    manual.actions.extend([
        draft_payload(tags=42), draft_payload(),
        review_payload(decision='REWRITE', scores=[4] * 6, total=24,
                       issues=['수정 요청'], rewrite_instructions='내용 정리'),
    ])
    with pytest.raises(ValueError, match='review'):
        run(manual)
    assert [c['text']['format']['name'] for c in manual.calls] == [
        'writer', 'pre_review_correction', 'reviewer']
    assert sum(c['max_output_tokens'] for c in manual.calls) == 30000
    assert run(manual)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(manual.calls) == 3


def test_explicit_truncation_retry_spends_shared_reserved_budget(manual):
    manual.actions.append(SimpleNamespace(
        id='offline-incomplete-response', model='gpt-5', status='incomplete', output=[],
        output_text='', incomplete_details={'reason': 'max_output_tokens'}, usage=None,
    ))
    assert run(manual)['status'] == 'MANUAL_DRAFT_READY'
    assert [c['max_output_tokens'] for c in manual.calls] == [12000, 16000, 6000]
    assert sum(c['max_output_tokens'] for c in manual.calls) == 34000
    journal = json.loads((manual.directory / 'paid-calls.json').read_text())
    assert len(journal) == 3
    assert run(manual)['status'] == 'MANUAL_DRAFT_READY'
    assert len(manual.calls) == 3


def test_truncation_cannot_expand_approved_output_budget(manual):
    packet = copy.deepcopy(manual.packet)
    packet['budget']['max_output_tokens'] = 18000
    manual.actions.append(SimpleNamespace(
        id='offline-incomplete-response', model='gpt-5', status='incomplete', output=[],
        output_text='', incomplete_details={'reason': 'max_output_tokens'}, usage=None,
    ))
    with pytest.raises(ValueError, match='budget'):
        run(manual, packet)
    assert len(manual.calls) == 1
    assert run(manual, packet)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(manual.calls) == 1


def test_missing_database_never_bootstraps_a_new_duplicate_namespace(manual):
    manual.settings.db_path.unlink()
    with pytest.raises(sqlite3.OperationalError, match='unable to open database'):
        run(manual)
    assert manual.calls == []
    assert not manual.settings.db_path.exists()


def test_stale_bundle_cannot_remove_local_manual_claims(manual, tmp_path):
    stale_bundle = tmp_path / 'old' / 'bundle.enc'
    cloud.pack(manual.settings, stale_bundle)
    run(manual)
    before = {p.relative_to(manual.settings.db_path.parent): p.read_bytes()
              for p in manual.settings.db_path.parent.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='Manual request checkpoint conflict'):
        cloud.extract_bundle(stale_bundle.read_bytes(), manual.settings.db_path.parent,
                             manual.key.decode())
    after = {p.relative_to(manual.settings.db_path.parent): p.read_bytes()
             for p in manual.settings.db_path.parent.rglob('*') if p.is_file()}
    assert after == before
    assert run(manual)['status'] == 'MANUAL_DRAFT_READY'
    assert len(manual.calls) == 2


@pytest.mark.parametrize('filename', ['claim.json', 'paid-calls.json', 'approved-packet.json'])
@pytest.mark.parametrize('change', ['remove', 'replace'])
def test_restoration_rejects_changed_or_missing_manual_spend_evidence(
        manual, tmp_path, filename, change):
    run(manual)
    destination = tmp_path / 'handoff' / 'bundle.enc'
    cloud.pack(manual.settings, destination)
    target = f'manual-requests/{REQUEST_ID}/{filename}'
    altered = io.BytesIO()
    with (zipfile.ZipFile(io.BytesIO(Fernet(manual.key).decrypt(destination.read_bytes()))) as old,
          zipfile.ZipFile(altered, 'w') as archive):
        for name in old.namelist():
            if name == target and change == 'remove':
                continue
            archive.writestr(name, b'{}' if name == target else old.read(name))
    encrypted = Fernet(manual.key).encrypt(altered.getvalue())
    before = {p.relative_to(manual.settings.db_path.parent): p.read_bytes()
              for p in manual.settings.db_path.parent.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='Manual request checkpoint conflict'):
        cloud.extract_bundle(encrypted, manual.settings.db_path.parent, manual.key.decode())
    after = {p.relative_to(manual.settings.db_path.parent): p.read_bytes()
             for p in manual.settings.db_path.parent.rglob('*') if p.is_file()}
    assert after == before
    assert len(manual.calls) == 2


def test_cloud_manual_dispatch_skips_daily_seed_selection_recovery_and_publishing(
        manual, monkeypatch, tmp_path, capsys):
    token, digest = manual.encode()
    manual_requests.reserve_manual_request(manual.settings, token, REQUEST_ID, digest)
    restored = []
    monkeypatch.setattr(cloud, 'load_settings', lambda: manual.settings)
    monkeypatch.setattr(cloud, 'restore', lambda directory: restored.append(directory))
    for function in ['seed_inputs', 'run_daily', 'recover_preparation', 'import_manual_saves']:
        monkeypatch.setattr(cloud, function, lambda *a, **kw: pytest.fail('No automatic pipeline'))
    monkeypatch.setenv('BLOG_MANUAL_REQUEST_PACKET', token)
    output = tmp_path / 'handoff' / 'bundle.enc'
    monkeypatch.setenv('BLOG_ENCRYPTED_OUTPUT', str(output))
    monkeypatch.setattr('sys.argv', ['cloud', 'manual-request', '--manual-request-id', REQUEST_ID,
                                    '--manual-request-sha256', digest])
    cloud.main()
    assert restored == [manual.settings.db_path.parent]
    assert output.exists()
    assert len(manual.calls) == 2
    stdout = capsys.readouterr().out
    assert 'MANUAL_DRAFT_READY' in stdout
    assert manual.packet['question'] not in stdout
    assert manual.packet['approval_reference'] not in stdout
    assert token not in stdout


@pytest.mark.parametrize('failure_point,expected_calls', [('reserve', 0), ('record_return', 1)])
def test_journal_write_failure_cannot_enable_paid_retries(
        manual, monkeypatch, failure_point, expected_calls):
    original = manual_requests.atomic_json

    def write(path, value):
        if (path.name == 'paid-calls.json'
                and (failure_point == 'reserve' or value[-1]['status'] == 'RETURNED')):
            raise OSError('Simulated checkpoint write failure')
        original(path, value)

    monkeypatch.setattr(manual_requests, 'atomic_json', write)
    with pytest.raises(OSError, match='checkpoint write failure'):
        run(manual)
    assert len(manual.calls) == expected_calls
    assert json.loads((manual.directory / 'claim.json').read_text())['status'] == 'HELD'
    assert run(manual)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(manual.calls) == expected_calls


def test_reservation_alone_is_nonpaid_and_bound_to_workflow_and_packet(manual):
    ciphertext, digest = manual.encode()
    before_db = manual.settings.db_path.read_bytes()
    result = manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    assert result == {'request_id': REQUEST_ID, 'status': 'MANUAL_REQUEST_RESERVED'}
    claim = json.loads((manual.directory / 'claim.json').read_text())
    assert claim['status'] == 'RESERVED' and claim['run_id'] == '123'
    assert claim['packet_sha256'] == digest
    assert claim['budget'] == manual.packet['budget']
    assert manual.calls == manual.remote_checks == []
    assert manual.settings.db_path.read_bytes() == before_db
    assert not (manual.directory / 'paid-calls.json').exists()
    assert not (manual.directory / 'result.json').exists()
    with pytest.raises(ValueError, match='already reserved'):
        manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    assert manual.calls == []


@pytest.mark.parametrize('name,value', [
    ('GITHUB_ACTIONS', ''), ('GITHUB_ACTIONS', 'false'), ('GITHUB_ACTIONS', 'True'),
    ('GITHUB_RUN_ID', ''), ('GITHUB_RUN_ID', 'not-a-run'),
    ('GITHUB_RUN_ATTEMPT', ''), ('GITHUB_RUN_ATTEMPT', '2'), ('GITHUB_RUN_ATTEMPT', '01'),
])
def test_reservation_requires_first_workflow_attempt(manual, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    ciphertext, digest = manual.encode()
    with pytest.raises(ValueError, match='first-attempt owner workflow'):
        manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    assert manual.calls == manual.remote_checks == []
    assert not manual.directory.exists()


def test_paid_runner_requires_previously_preserved_reservation(manual):
    ciphertext, digest = manual.encode()
    with pytest.raises(ValueError, match='reservation is required'):
        manual_requests.run_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    assert manual.calls == manual.remote_checks == []
    assert not manual.directory.exists()


def test_new_workflow_cannot_consume_other_runs_remote_reservation(manual, monkeypatch):
    ciphertext, digest = manual.encode()
    manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    claim = (manual.directory / 'claim.json').read_bytes()
    monkeypatch.setenv('GITHUB_RUN_ID', '456')
    assert manual_requests.run_manual_request(manual.settings, ciphertext, REQUEST_ID, digest) == {
        'request_id': REQUEST_ID, 'status': 'MANUAL_CHECK_REQUIRED'}
    assert manual.calls == manual.remote_checks == []
    assert (manual.directory / 'claim.json').read_bytes() == claim


def test_workflow_rerun_cannot_consume_unfinished_reservation(manual, monkeypatch):
    ciphertext, digest = manual.encode()
    manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
    with pytest.raises(ValueError, match='first-attempt owner workflow'):
        manual_requests.run_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    assert manual.calls == manual.remote_checks == []


@pytest.mark.parametrize('invalid', ['missing', 'expired', 'unknown_expiry', 'other_run', 'ordinary'])
def test_paid_runner_requires_exact_nonexpired_remote_reservation(manual, monkeypatch, invalid):
    ciphertext, digest = manual.encode()
    manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    before = (manual.directory / 'claim.json').read_bytes()
    name = manual_requests.reservation_name('123', manual.packet)
    artifacts = [{'name': name, 'expired': False}]
    if invalid == 'missing':
        artifacts = []
    elif invalid == 'expired':
        artifacts[0]['expired'] = True
    elif invalid == 'unknown_expiry':
        del artifacts[0]['expired']
    elif invalid == 'other_run':
        artifacts[0]['name'] = manual_requests.reservation_name('456', manual.packet)
    else:
        artifacts[0]['name'] = 'blog-state-123-1'
    monkeypatch.setattr(cloud, 'github_get', lambda path: json.dumps({'artifacts': artifacts}).encode())
    with pytest.raises(ValueError, match='reservation upload is not verified'):
        manual_requests.run_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    assert manual.calls == []
    assert (manual.directory / 'claim.json').read_bytes() == before
    assert not (manual.directory / 'paid-calls.json').exists()


def test_remote_reservation_read_failure_never_reaches_model(manual, monkeypatch):
    ciphertext, digest = manual.encode()
    manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)

    def unavailable(path):
        raise OSError('Artifact read unavailable')

    monkeypatch.setattr(cloud, 'github_get', unavailable)
    with pytest.raises(OSError, match='Artifact read unavailable'):
        manual_requests.run_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    assert manual.calls == []
    assert json.loads((manual.directory / 'claim.json').read_text())['status'] == 'RESERVED'


def test_cancelled_fresh_runner_cannot_repeat_remotely_reserved_work(
        manual, monkeypatch, tmp_path):
    ciphertext, digest = manual.encode()
    manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    remote = tmp_path / 'reservation' / 'bundle.enc'
    cloud.pack(manual.settings, remote)
    manual.actions.append(TimeoutError('Runner lost after submission'))
    with pytest.raises(TimeoutError):
        manual_requests.run_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    assert len(manual.calls) == 1
    # A new machine only has the pre-spend artifact, not the lost local HELD journal.
    fresh = tmp_path / 'fresh-runner'
    cloud.extract_bundle(remote.read_bytes(), fresh, manual.key.decode())
    settings = replace(manual.settings, db_path=fresh / 'blog.db',
                       artifact_dir=fresh / 'drafts', inbox_dir=fresh / 'inbox')
    monkeypatch.setenv('GITHUB_RUN_ID', '456')
    assert manual_requests.run_manual_request(settings, ciphertext, REQUEST_ID, digest) == {
        'request_id': REQUEST_ID, 'status': 'MANUAL_CHECK_REQUIRED'}
    assert len(manual.calls) == 1
    with pytest.raises(ValueError, match='already reserved'):
        manual_requests.reserve_manual_request(settings, ciphertext, REQUEST_ID, digest)
    assert len(manual.calls) == 1


@pytest.mark.parametrize('tampering', ['body', 'review', 'photo_metadata', 'approval'])
def test_cached_result_content_integrity_is_rechecked_without_paid_repair(manual, tampering):
    run(manual)
    path = manual.directory / 'result.json'
    result = json.loads(path.read_text())
    if tampering == 'body':
        result['post']['body'] += '\n승인되지 않은 추가 문장'
    elif tampering == 'review':
        result['review']['scores'] = [1] * 6
    elif tampering == 'photo_metadata':
        result['post']['photos'][0]['approved'] = False
    else:
        result['approval']['scope'] = 'publish'
    path.write_text(json.dumps(result))
    with pytest.raises(ValueError, match='integrity'):
        run(manual)
    assert len(manual.calls) == 2


@pytest.mark.parametrize('malformed', ['signature_only', 'truncated', 'dimensions'])
def test_thumbnail_is_decoded_before_reservation_or_paid_work(manual, malformed):
    if malformed == 'signature_only':
        data = b'\x89PNG\r\n\x1a\nnot an image'
    elif malformed == 'truncated':
        data = IMAGE[:40]
    else:
        buffer = io.BytesIO()
        Image.new('RGB', (4097, 1), '#abcdef').save(buffer, format='PNG')
        data = buffer.getvalue()
    packet = copy.deepcopy(manual.packet)
    packet['thumbnail'].update(data_base64=base64.b64encode(data).decode(),
                               sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises((ValueError, OSError)):
        run(manual, packet)
    assert manual.calls == []
    assert not manual.directory.exists()


def test_cloud_reserve_dispatch_packs_claim_without_any_generation(
        manual, monkeypatch, tmp_path, capsys):
    token, digest = manual.encode()
    monkeypatch.setattr(cloud, 'load_settings', lambda: manual.settings)
    monkeypatch.setattr(cloud, 'restore', lambda directory: None)
    for function in ['seed_inputs', 'run_daily', 'recover_preparation', 'import_manual_saves']:
        monkeypatch.setattr(cloud, function, lambda *a, **kw: pytest.fail('No automatic pipeline'))
    monkeypatch.setenv('BLOG_MANUAL_REQUEST_PACKET', token)
    output = tmp_path / 'reservation' / 'bundle.enc'
    monkeypatch.setenv('BLOG_ENCRYPTED_OUTPUT', str(output))
    monkeypatch.setattr('sys.argv', ['cloud', 'manual-request-reserve',
                                    '--manual-request-id', REQUEST_ID,
                                    '--manual-request-sha256', digest])
    cloud.main()
    assert output.exists()
    assert manual.calls == manual.remote_checks == []
    with zipfile.ZipFile(io.BytesIO(Fernet(manual.key).decrypt(output.read_bytes()))) as archive:
        claim = json.loads(archive.read(f'manual-requests/{REQUEST_ID}/claim.json'))
        assert claim['status'] == 'RESERVED'
        assert claim['run_id'] == '123'
        assert json.loads(archive.read('ready.json'))['posts'] == []
    assert 'MANUAL_REQUEST_RESERVED' in capsys.readouterr().out


@pytest.mark.parametrize('identity', ['request_id', 'approval_reference', 'question'])
@pytest.mark.parametrize('expired', [False, True])
def test_remote_history_blocks_each_consumed_identity_on_second_page(
        manual, monkeypatch, identity, expired):
    historical = {**manual.packet, 'request_id': 'older-request',
                  'approval_reference': 'Sentinel_older_owner_approval',
                  'question': '다른 글의 이름은 무슨 뜻인가요?'}
    historical[identity] = manual.packet[identity]
    if identity == 'question':
        historical[identity] = '\n  ' + historical[identity].replace(' ', '\n') + ' '
    artifact_name = manual_requests.reservation_name('999', historical)
    queried = []

    def history(path):
        queried.append(path)
        page = int(path.rsplit('=', 1)[-1])
        if page == 1:
            return json.dumps({'artifacts': [{'name': f'unrelated-{i}'}
                                            for i in range(100)]}).encode()
        assert page == 2
        return json.dumps({'artifacts': [{'name': artifact_name, 'expired': expired}]}).encode()

    monkeypatch.setattr(cloud, 'github_get', history)
    with pytest.raises(ValueError, match='Remote manual reservation already consumes'):
        run(manual)
    # Inline packets first rule out a lost thumbnail project, then scan text reservations.
    assert queried == ['/actions/artifacts?per_page=100&page=1',
                       '/actions/artifacts?per_page=100&page=2'] * 2
    assert manual.calls == []
    assert not manual.directory.exists()


@pytest.mark.parametrize('failure', ['io_error', 'invalid_json', 'missing_artifacts'])
def test_history_lookup_failure_is_never_interpreted_as_empty(manual, monkeypatch, failure):
    def history(path):
        if failure == 'io_error':
            raise OSError('GitHub history unavailable')
        return b'not JSON' if failure == 'invalid_json' else b'{}'

    monkeypatch.setattr(cloud, 'github_get', history)
    with pytest.raises((OSError, ValueError, KeyError)):
        run(manual)
    assert manual.calls == []
    assert not manual.directory.exists()


@pytest.mark.parametrize('artifacts', [{}, None, '', [None], ['not-an-artifact']])
@pytest.mark.parametrize('phase', ['history', 'paid_verification'])
def test_malformed_remote_artifact_container_fails_closed(manual, monkeypatch, artifacts, phase):
    ciphertext, digest = manual.encode()
    if phase == 'paid_verification':
        manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    monkeypatch.setattr(cloud, 'github_get', lambda path: json.dumps({'artifacts': artifacts}).encode())
    action = (manual_requests.reserve_manual_request if phase == 'history'
              else manual_requests.run_manual_request)
    with pytest.raises(ValueError, match='Malformed remote'):
        action(manual.settings, ciphertext, REQUEST_ID, digest)
    assert manual.calls == []
    assert not (manual.directory / 'paid-calls.json').exists()


def test_incomplete_remote_history_never_authorizes_new_paid_namespace(manual, monkeypatch):
    pages = []

    def history(path):
        pages.append(path)
        return json.dumps({'artifacts': [{'name': f'unrelated-{i}'} for i in range(100)]}).encode()

    monkeypatch.setattr(cloud, 'github_get', history)
    with pytest.raises(ValueError, match='history is incomplete'):
        run(manual)
    assert len(pages) == 100
    assert manual.calls == []
    assert not manual.directory.exists()


def test_artifact_identity_contains_only_opaque_hashes(manual):
    name = manual_requests.reservation_name('123', manual.packet)
    assert name.startswith('blog-state-123-manual-')
    assert len(name.split('-manual-')[1].split('-')) == 3
    for field in ['request_id', 'approval_reference', 'question']:
        assert manual.packet[field] not in name
    changed = {**manual.packet, 'context': 'Additional unchanged approval context'}
    assert manual_requests.reservation_name('123', changed) == name


def test_older_bundle_cannot_erase_remote_paid_claim_on_fresh_machine(
        manual, monkeypatch, tmp_path):
    stale = tmp_path / 'old' / 'bundle.enc'
    cloud.pack(manual.settings, stale)
    run(manual)
    historical = manual_requests.reservation_name('123', manual.packet)
    fresh = tmp_path / 'fresh-from-old-artifact'
    cloud.extract_bundle(stale.read_bytes(), fresh, manual.key.decode())
    settings = replace(manual.settings, db_path=fresh / 'blog.db',
                       artifact_dir=fresh / 'drafts', inbox_dir=fresh / 'inbox')
    assert not (fresh / 'manual-requests' / REQUEST_ID).exists()
    monkeypatch.setenv('GITHUB_RUN_ID', '456')
    monkeypatch.setattr(cloud, 'github_get', lambda path: json.dumps({'artifacts': [
        {'name': historical, 'expired': False}]}).encode())
    ciphertext, digest = manual.encode()
    with pytest.raises(ValueError, match='Remote manual reservation already consumes'):
        manual_requests.reserve_manual_request(settings, ciphertext, REQUEST_ID, digest)
    assert not (fresh / 'manual-requests' / REQUEST_ID).exists()
    assert len(manual.calls) == 2


def test_completed_cached_reservation_does_not_lookup_history_or_require_new_run(
        manual, monkeypatch, tmp_path):
    run(manual)
    monkeypatch.setattr(cloud, 'github_get', lambda path: pytest.fail('No cache replay lookup'))
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
    output = tmp_path / 'github-output.txt'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    ciphertext, digest = manual.encode()
    result = manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    assert result == {'request_id': REQUEST_ID, 'status': 'MANUAL_DRAFT_READY', 'cached': True}
    assert output.read_text() == 'cached=true\nartifact_name=\n'
    assert len(manual.calls) == 2


def test_new_reservation_exports_indexed_artifact_name_before_paid_work(
        manual, monkeypatch, tmp_path):
    output = tmp_path / 'github-output.txt'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    ciphertext, digest = manual.encode()
    manual_requests.reserve_manual_request(manual.settings, ciphertext, REQUEST_ID, digest)
    expected_name = manual_requests.reservation_name('123', manual.packet)
    assert output.read_text() == f'cached=false\nartifact_name={expected_name}\n'
    assert manual.calls == []


def test_prior_manual_title_is_considered_before_reviewing_new_request(manual, monkeypatch):
    run(manual)
    packet = {**manual.packet, 'request_id': 'separate-manual-request',
              'approval_reference': 'Sentinel_another_explicit_owner_request',
              'question': '전혀 다른 음식 이름은 무슨 뜻인가요?'}
    ciphertext, digest = manual.encode(packet)
    manual_requests.reserve_manual_request(manual.settings, ciphertext, packet['request_id'], digest)
    monkeypatch.setattr(cloud, 'github_get', lambda path: json.dumps({'artifacts': [
        {'name': manual_requests.reservation_name('123', packet), 'expired': False}]}).encode())
    # The writer mistakenly returns exactly the first manual title for this different question.
    with pytest.raises(ValueError, match='Duplicate manual draft title'):
        manual_requests.run_manual_request(manual.settings, ciphertext, packet['request_id'], digest)
    assert len(manual.calls) == 3
    assert manual.calls[-1]['text']['format']['name'] == 'writer'


def test_workflow_uploads_reservation_before_generation_and_skips_paid_cache_replays():
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/blog-prepare.yml').read_text()
    reservation = workflow.index('name: Reserve explicitly approved manual request')
    upload = workflow.index('name: Persist manual reservation before paid work')
    paid = workflow.index('name: Prepare or resume')
    assert reservation < upload < paid
    assert 'id: manual_reservation' in workflow[reservation:upload]
    assert 'actions/upload-artifact@v6' in workflow[upload:paid]
    assert 'steps.manual_reservation.outputs.artifact_name' in workflow[upload:paid]
    assert "steps.manual_reservation.outputs.cached != 'true'" in workflow[upload:paid]
    assert "steps.manual_reservation.outputs.cached != 'true'" in workflow[paid:]
    assert 'github.actor == github.repository_owner' in workflow
    assert 'cancel-in-progress: false' in workflow


@pytest.mark.parametrize('missing', ['budget_mode', 'max_estimated_usd', 'cost_approval_reference'])
def test_old_call_ceiling_packet_without_cost_consent_cannot_be_reserved(manual, missing):
    packet = copy.deepcopy(manual.packet)
    del packet['budget'][missing]
    with pytest.raises(ValueError):
        run(manual, packet)
    assert manual.calls == manual.constructors == manual.history_checks == []
    assert not manual.directory.exists()


@pytest.mark.parametrize('entrypoint', ['validate', 'reserve', 'paid'])
def test_strict_dollar_cap_always_holds_before_any_preparation(manual, entrypoint):
    from blogbot.manual_costs import CostBoundUnavailable

    packet = copy.deepcopy(manual.packet)
    packet['budget']['budget_mode'] = 'strict_usd'
    ciphertext, digest = manual.encode(packet)
    action = {'validate': manual_requests.validate_packet,
              'reserve': manual_requests.reserve_manual_request,
              'paid': manual_requests.run_manual_request}[entrypoint]
    with pytest.raises(CostBoundUnavailable):
        action(manual.settings, ciphertext, REQUEST_ID, digest)
    assert manual.calls == manual.constructors == manual.history_checks == []
    assert not manual.directory.exists()


def test_estimated_cost_consent_is_retained_without_changing_hard_call_caps(manual):
    run(manual)
    claim = json.loads((manual.directory / 'claim.json').read_text())
    result = json.loads((manual.directory / 'result.json').read_text())
    for budget in [claim['budget'], result['approval']['budget']]:
        assert budget['budget_mode'] == 'estimated_with_call_caps'
        assert budget['max_estimated_usd'] == 3
        assert budget['cost_approval_reference'] == 'Sentinel_test_estimated_cost_approval'
        assert budget['max_calls'] == 4 and budget['max_output_tokens'] == 36000
    assert len(manual.calls) == 2


def additional_receipt(manual, **changes):
    claim = json.loads((manual.directory / 'claim.json').read_text())
    return {'request_id': REQUEST_ID, 'day': DAY, 'category': 'origins',
            'status': 'SAVED_NAVER', 'approval_reference': claim['approval_reference'],
            'result_sha256': claim['result_sha256'], **changes}


def test_additional_save_preserves_october_11_100day_topic_and_ordinary_quota(manual, monkeypatch):
    run(manual)

    class NextDay(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 10, 11, 3, tzinfo=UTC)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr('blogbot.core.datetime', NextDay)
    settings = load_settings()
    plan = settings.config['daily_plan']
    topic = copy.deepcopy(settings.config['topics']['scheduled']['2026-10-11'])
    assert plan['category'] == 'parenting' and plan['target'] == 1
    assert topic['id'] == 'owner-20261011-100day-baby-play'
    receipt = additional_receipt(manual, day='2026-10-11')
    payload = {'verified_date': '2026-10-11', 'records': [], 'additional_records': [receipt]}
    before_db = settings.db_path.read_bytes()
    manual_requests.reconcile_additional_receipts(settings, payload)
    assert settings.db_path.read_bytes() == before_db
    assert settings.config['topics']['scheduled']['2026-10-11'] == topic
    with closing(connect_db(settings.db_path)) as conn:
        assert saved_count(conn, plan) == 0
        import_work_receipts(conn, {'verified_date': '2026-10-11', 'records': [
            {'request_id': topic['id'], 'day': '2026-10-11', 'category': 'parenting',
             'status': 'SAVED_NAVER'}]}, blog_id='offline_owner',
                             categories=settings.config['categories'])
        assert saved_count(conn, plan) == 1
    before_db = settings.db_path.read_bytes()
    manual_requests.reconcile_additional_receipts(settings, payload)
    assert settings.db_path.read_bytes() == before_db
    assert json.loads((manual.directory / 'save-receipt.json').read_text())['day'] == '2026-10-11'


@pytest.mark.parametrize('change', [
    {'approval_reference': 'Sentinel_wrong_manual_result_reference'},
    {'result_sha256': '0' * 64}, {'category': 'parenting'}, {'status': 'PUBLISHED'},
])
def test_extra_save_requires_exact_retained_manual_result_authority(manual, change):
    run(manual)
    receipt = additional_receipt(manual, **change)
    with pytest.raises(ValueError):
        manual_requests.reconcile_additional_receipts(manual.settings, {
            'verified_date': DAY, 'records': [], 'additional_records': [receipt]})
    assert not (manual.directory / 'save-receipt.json').exists()


def test_flag_cannot_reclassify_an_ordinary_receipt_out_of_daily_count(manual):
    run(manual)
    ordinary = {'request_id': 'ordinary-owner-request', 'day': DAY, 'category': 'parenting',
                'status': 'SAVED_NAVER', 'additional': True, 'manual_additional': True}
    payload = {'verified_date': DAY, 'records': [ordinary]}
    manual_requests.reconcile_additional_receipts(manual.settings, payload)
    with closing(connect_db(manual.settings.db_path)) as conn:
        import_work_receipts(conn, payload, blog_id='offline_owner',
                             categories=manual.settings.config['categories'])
        assert saved_count(conn, manual.settings.config['daily_plan']) == 1
    fake_extra = {**ordinary, 'category': 'origins',
                  'approval_reference': manual.packet['approval_reference'], 'result_sha256': '0' * 64}
    with pytest.raises((ValueError, FileNotFoundError)):
        manual_requests.reconcile_additional_receipts(manual.settings, {
            'verified_date': DAY, 'records': [], 'additional_records': [fake_extra]})
    with closing(connect_db(manual.settings.db_path)) as conn:
        assert saved_count(conn, manual.settings.config['daily_plan']) == 1


def test_same_identity_cannot_be_both_ordinary_and_extra(manual):
    run(manual)
    receipt = additional_receipt(manual)
    with pytest.raises(ValueError, match='ordinary quota|reclassified'):
        manual_requests.reconcile_additional_receipts(manual.settings, {
            'verified_date': DAY, 'records': [receipt], 'additional_records': [receipt]})
    assert not (manual.directory / 'save-receipt.json').exists()


@pytest.mark.parametrize('status', ['SAVING', 'SAVE_UNCERTAIN'])
def test_pending_extra_save_blocks_even_when_new_ledger_omits_it(manual, status):
    run(manual)
    payload = {'verified_date': DAY, 'records': [],
               'additional_records': [additional_receipt(manual, status=status)]}
    with pytest.raises(ValueError, match='Uncertain additional save'):
        manual_requests.reconcile_additional_receipts(manual.settings, payload)
    stored = (manual.directory / 'save-receipt.json').read_bytes()
    with pytest.raises(ValueError, match='Uncertain additional save'):
        manual_requests.reconcile_additional_receipts(manual.settings, {
            'verified_date': DAY, 'records': []})
    assert (manual.directory / 'save-receipt.json').read_bytes() == stored
    payload['additional_records'][0]['status'] = 'SAVED_NAVER'
    manual_requests.reconcile_additional_receipts(manual.settings, payload)
    assert json.loads((manual.directory / 'save-receipt.json').read_text())['status'] == 'SAVED_NAVER'


@pytest.mark.parametrize('change', [{'day': '2026-10-09'}, {'status': 'SAVING'},
                                  {'status': 'SAVE_UNCERTAIN'}])
def test_saved_extra_receipt_day_and_completion_cannot_be_reset(manual, change):
    run(manual)
    receipt = additional_receipt(manual)
    manual_requests.reconcile_additional_receipts(manual.settings, {
        'verified_date': DAY, 'records': [], 'additional_records': [receipt]})
    before = (manual.directory / 'save-receipt.json').read_bytes()
    with pytest.raises(ValueError, match='cannot be reset'):
        manual_requests.reconcile_additional_receipts(manual.settings, {
            'verified_date': DAY, 'records': [], 'additional_records': [{**receipt, **change}]})
    assert (manual.directory / 'save-receipt.json').read_bytes() == before


def test_extra_save_receipt_is_encrypted_and_stale_restore_cannot_remove_it(manual, tmp_path):
    run(manual)
    before_save = tmp_path / 'before' / 'bundle.enc'
    cloud.pack(manual.settings, before_save)
    manual_requests.reconcile_additional_receipts(manual.settings, {
        'verified_date': DAY, 'records': [], 'additional_records': [additional_receipt(manual)]})
    saved = (manual.directory / 'save-receipt.json').read_bytes()
    cloud.extract_bundle(before_save.read_bytes(), manual.settings.db_path.parent,
                         manual.key.decode())
    assert (manual.directory / 'save-receipt.json').read_bytes() == saved
    after_save = tmp_path / 'after' / 'bundle.enc'
    cloud.pack(manual.settings, after_save)
    with zipfile.ZipFile(io.BytesIO(Fernet(manual.key).decrypt(after_save.read_bytes()))) as archive:
        assert archive.read(f'manual-requests/{REQUEST_ID}/save-receipt.json') == saved


def test_observed_not_saved_resolution_clears_extra_hold_without_resetting_identity(manual):
    run(manual)
    settings = replace(manual.settings, naver_blog_id='offline_owner')
    receipt = additional_receipt(manual, status='SAVE_UNCERTAIN')
    payload = {'verified_date': DAY, 'records': [], 'additional_records': [receipt]}
    with pytest.raises(ValueError, match='Uncertain additional save'):
        manual_requests.reconcile_additional_receipts(settings, payload)
    resolved = {**receipt, 'status': 'SAVE_NOT_SAVED', 'save_check': {
        'resolution': 'not-saved', 'draft_list_checked': True, 'published_list_checked': True,
        'draft_absent': True, 'published_absent': True, 'evidence_ref': 'offline-verified-list-check',
        'attempted_at': '2026-10-10T10:00:00+09:00',
        'checked_at': '2026-10-10T11:00:00+09:00',
        'blog_url': 'https://blog.naver.com/offline_owner'}}
    manual_requests.reconcile_additional_receipts(settings, {
        'verified_date': DAY, 'records': [], 'additional_records': [resolved]})
    manual_requests.reconcile_additional_receipts(settings, {'verified_date': DAY, 'records': []})
    assert json.loads((manual.directory / 'save-receipt.json').read_text())['status'] == 'SAVE_NOT_SAVED'
    with pytest.raises(ValueError, match='cannot be reset'):
        manual_requests.reconcile_additional_receipts(settings, {
            'verified_date': DAY, 'records': [], 'additional_records': [additional_receipt(manual)]})


def test_not_saved_label_without_observed_absence_cannot_clear_extra_hold(manual):
    run(manual)
    with pytest.raises(ValueError, match='absence evidence'):
        manual_requests.reconcile_additional_receipts(manual.settings, {
            'verified_date': DAY, 'records': [],
            'additional_records': [additional_receipt(manual, status='SAVE_NOT_SAVED')]})
    assert not (manual.directory / 'save-receipt.json').exists()


@pytest.mark.parametrize('status', ['SAVING', 'SAVE_UNCERTAIN', 'SAVED_NAVER'])
def test_any_additional_save_history_blocks_cached_new_delivery(manual, status):
    run(manual)
    payload = {'verified_date': DAY, 'records': [],
               'additional_records': [additional_receipt(manual, status=status)]}
    if status == 'SAVED_NAVER':
        manual_requests.reconcile_additional_receipts(manual.settings, payload)
    else:
        with pytest.raises(ValueError, match='Uncertain additional save'):
            manual_requests.reconcile_additional_receipts(manual.settings, payload)
    ciphertext, digest = manual.encode()
    for handler in [manual_requests.reserve_manual_request, manual_requests.run_manual_request]:
        with pytest.raises(ValueError, match='save history'):
            handler(manual.settings, ciphertext, REQUEST_ID, digest)
    assert len(manual.calls) == 2


def test_fresh_ordinary_resolution_does_not_deadlock_extra_receipt_reconciliation(manual):
    run(manual)
    with closing(connect_db(manual.settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO save_receipts(request_id,day,category,status) VALUES(?,?,?,?)',
                     ('ordinary-pending', DAY, 'parenting', 'SAVE_UNCERTAIN'))
    payload = {'verified_date': DAY, 'records': [
        {'request_id': 'ordinary-pending', 'day': DAY, 'category': 'parenting',
         'status': 'SAVED_NAVER'}], 'additional_records': [additional_receipt(manual)]}
    manual_requests.reconcile_additional_receipts(manual.settings, payload)
    with closing(connect_db(manual.settings.db_path)) as conn:
        import_work_receipts(conn, payload, blog_id='offline_owner',
                             categories=manual.settings.config['categories'])
        assert saved_count(conn, manual.settings.config['daily_plan']) == 1
    assert json.loads((manual.directory / 'save-receipt.json').read_text())['status'] == 'SAVED_NAVER'


@pytest.mark.parametrize('changes', [{'day': '2026-10-09'}, {'status': 'SAVE_NOT_SAVED'}])
def test_restore_rejects_conflicting_extra_receipt_identity_or_terminal_status(
        manual, tmp_path, changes):
    run(manual)
    manual_requests.reconcile_additional_receipts(manual.settings, {
        'verified_date': DAY, 'records': [], 'additional_records': [additional_receipt(manual)]})
    original = (manual.directory / 'save-receipt.json').read_bytes()
    destination = tmp_path / 'handoff' / 'bundle.enc'
    cloud.pack(manual.settings, destination)
    target = f'manual-requests/{REQUEST_ID}/save-receipt.json'
    modified = io.BytesIO()
    with (zipfile.ZipFile(io.BytesIO(Fernet(manual.key).decrypt(destination.read_bytes()))) as old,
          zipfile.ZipFile(modified, 'w') as archive):
        for name in old.namelist():
            content = old.read(name)
            if name == target:
                content = json.dumps({**json.loads(content), **changes}).encode()
            archive.writestr(name, content)
    with pytest.raises(ValueError, match='Conflicting additional receipt'):
        cloud.extract_bundle(Fernet(manual.key).encrypt(modified.getvalue()),
                             manual.settings.db_path.parent, manual.key.decode())
    assert (manual.directory / 'save-receipt.json').read_bytes() == original
