"""Offline review-only recovery never retroactively approves a held manuscript."""
import base64
import copy
import hashlib
import io
import json
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken
from PIL import Image

from blogbot import cloud, manual_requests, manual_review
from blogbot.config import load_settings
from blogbot.core import connect_db
from blogbot.llm import _cached_review_request, _checked_review
from blogbot.planning import MediaBusy, media_claim

DAY = '2026-10-11'
REQUEST_ID = 'owner-held-origins-20261011'
SOURCE = 'https://stdict.korean.go.kr/search/searchView.do?word_no=123'
EXCERPT = '공기는 밥을 담는 작은 그릇이다.'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def review(**changes):
    return {'scores': [5, 5, 5, 5, 4, 5], 'total': 29, 'decision': 'PASS',
            'issues': [], 'blocking_issues': [], 'rewrite_instructions': '',
            'source_checks': [{'claim': '공기의 뜻', 'source_url': SOURCE,
                               'evidence': EXCERPT, 'status': 'SUPPORTED'}], **changes}


@pytest.fixture
def state(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 10, 11, 3, tzinfo=UTC)
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    monkeypatch.setattr('blogbot.core.datetime', Clock)
    monkeypatch.setattr(manual_review, 'datetime', Clock)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path / 'state'))
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-test-placeholder')
    monkeypatch.setenv('OPENAI_MODEL', 'gpt-5')
    monkeypatch.setenv('OPENAI_REVIEW_MODEL', 'gpt-5')
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_RUN_ID', '123')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
    monkeypatch.setenv('BLOG_NOTIFY_ENABLED', 'false')
    settings = load_settings()
    root = settings.db_path.parent
    with closing(connect_db(settings.db_path)):
        pass
    folder = root / 'manual-requests' / REQUEST_ID
    folder.mkdir(parents=True)
    image = io.BytesIO()
    Image.new('RGB', (24, 24), '#d9e6ec').save(image, format='JPEG')
    image_bytes = image.getvalue()
    (folder / 'thumbnail.jpg').write_bytes(image_bytes)
    budget = {'writer_model': 'gpt-5', 'review_model': 'gpt-5', 'max_calls': 4,
              'max_output_tokens': 36000, 'budget_mode': 'estimated_with_call_caps',
              'max_estimated_usd': 3,
              'cost_approval_reference': 'Sentinel_test_original_estimate_consent'}
    original = {'version': 'manual-request-v1', 'request_id': REQUEST_ID, 'date': DAY,
                'approval_reference': 'Sentinel_test_original_article_approval',
                'approved_by': 'repository_owner', 'scope': 'draft_only', 'category': 'origins',
                'question': '공깃밥의 공기는 무슨 뜻인가요?', 'context': '사전의 뜻을 설명해주세요.',
                'sources': [{'url': SOURCE, 'excerpt': EXCERPT}], 'budget': budget,
                'thumbnail': {'data_base64': base64.b64encode(image_bytes).decode(),
                              'extension': 'jpg', 'sha256': hashlib.sha256(image_bytes).hexdigest(),
                              'approved': True, 'generated': True, 'role': 'thumbnail', 'caption': ''}}
    claim = {'request_id': REQUEST_ID, 'date': DAY, 'status': 'HELD',
             'approval_reference': original['approval_reference'], 'budget': budget,
             'scope': 'draft_only', 'packet_sha256': 'a' * 64, 'run_id': '111'}
    calls = [{'ordinal': i + 1, 'model': 'gpt-5', 'max_output_tokens': amount,
              'status': 'RETURNED', 'accounted_cost_usd': '0.05', 'cost_basis': 'reported_usage'}
             for i, amount in enumerate([12000, 6000, 12000, 6000])]
    for name, value in [('claim.json', claim), ('approved-packet.json', original),
                        ('paid-calls.json', calls)]:
        (folder / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))
    draft = {'title': '공깃밥의 공기는 무엇일까요?', 'subcategory': '음식·생활',
             'body': '공기는 밥을 담는 작은 그릇을 뜻합니다.\n\n공깃밥은 그 그릇에 담은 밥입니다.',
             'tags': ['공깃밥'], 'source_urls': [SOURCE], 'as_of_date': DAY}
    cache_dir = root / 'response-cache'
    cache_dir.mkdir()
    response_ids = ['original-writer', 'original-review-1', 'original-rewrite', 'original-review-2']
    stages = ['writer', 'reviewer', 'rewrite', 'reviewer']
    history = []
    for i, (stage, response_id) in enumerate(zip(stages, response_ids, strict=True)):
        payload = review() if stage == 'reviewer' else draft
        response = {'id': response_id, 'status': 'completed', 'model': 'gpt-5',
                    'output': [{'type': 'web_search_call', 'status': 'completed',
                                'action': {'type': 'search', 'sources': [{'url': SOURCE}]}}]}
        (cache_dir / f'{DAY}-{i}.json').write_text(json.dumps({
            'payload': payload, 'response': response}, ensure_ascii=False))
        history.append({'request_id': REQUEST_ID, 'stage': stage, 'response_id': response_id,
                        'model': 'gpt-5', 'status': 'completed', 'error': None})
    (root / 'usage.jsonl').write_text('\n'.join(json.dumps(row) for row in history) + '\n')
    source_text = '원문 사전 항목입니다. ' + EXCERPT + '\n관련 표기를 설명합니다.'
    packet = {'version': 'manual-review-v1', 'request_id': REQUEST_ID, 'date': DAY,
              'approval_reference': 'Sentinel_test_separate_one_review_approval',
              'original_approval_reference': original['approval_reference'],
              'approved_by': 'repository_owner', 'scope': 'review_only',
              'question': original['question'], 'budget': copy.deepcopy(budget),
              'original_claim_sha256': hashlib.sha256((folder / 'claim.json').read_bytes()).hexdigest(),
              'original_calls_sha256': hashlib.sha256((folder / 'paid-calls.json').read_bytes()).hexdigest(),
              'cache_file': f'{DAY}-2.json', 'writer_payload_sha256': digest(draft),
              'writer_response_id': 'original-rewrite',
              'evidence': [{'url': SOURCE, 'text': source_text,
                            'sha256': hashlib.sha256(source_text.encode()).hexdigest(),
                            'retrieved_at': '2026-10-11T11:00:00+09:00', 'verified_by': 'operator'}]}
    key = Fernet.generate_key()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key.decode())
    paid, outcomes, constructors, remote = [], [], [], []

    def create(**kwargs):
        paid.append(kwargs)
        once = folder / 'review-once'
        assert json.loads((once / 'claim.json').read_text())['status'] == 'STARTED'
        assert json.loads((once / 'paid-calls.json').read_text())[-1]['status'] == 'STARTED'
        outcome = outcomes.pop(0) if outcomes else review()
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, SimpleNamespace):
            return outcome
        raw = {'id': 'fresh-one-shot-review', 'model': 'gpt-5', 'status': 'completed',
               'output_text': json.dumps(outcome), 'output': [],
               'usage': {'input_tokens': 1000, 'output_tokens': 200}}
        return SimpleNamespace(**raw, model_dump=lambda: raw)

    def client(**kwargs):
        constructors.append(kwargs)
        return SimpleNamespace(responses=SimpleNamespace(create=create))

    monkeypatch.setattr('blogbot.llm.OpenAI', client)
    monkeypatch.setattr('blogbot.images.OpenAI', lambda **kw: pytest.fail('No images in review-only'))
    for method in ['create_draft', 'rewrite', 'correct_draft']:
        monkeypatch.setattr('blogbot.llm.BlogLLM.' + method,
                            lambda *a, **kw: pytest.fail('No manuscript generation in review-only'))
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)

    def github(path):
        remote.append(path)
        if path == '/actions/artifacts?per_page=100&page=1':
            return b'{"artifacts": []}'
        assert path == '/actions/runs/123/artifacts?per_page=100'
        return json.dumps({'artifacts': [{'name': manual_requests.reservation_name('123', packet, 'review'),
                                         'expired': False}]}).encode()

    monkeypatch.setattr(cloud, 'github_get', github)

    def encode(value=None):
        raw = json.dumps(packet if value is None else value, ensure_ascii=False).encode()
        return Fernet(key).encrypt(raw).decode(), hashlib.sha256(raw).hexdigest()

    return SimpleNamespace(settings=settings, root=root, folder=folder, packet=packet,
                           original=original, original_claim=claim, original_calls=calls,
                           draft=draft, key=key, paid=paid, outcomes=outcomes,
                           constructors=constructors, remote=remote, encode=encode)


def run(state, packet=None):
    token, pin = state.encode(packet)
    if not (state.folder / 'review-once' / 'claim.json').exists():
        manual_review.reserve_review(state.settings, token, REQUEST_ID, pin)
    return manual_review.run_review(state.settings, token, REQUEST_ID, pin)


def test_separate_review_reuses_exact_manuscript_and_preserves_original_history(state):
    original_claim = (state.folder / 'claim.json').read_bytes()
    original_calls = (state.folder / 'paid-calls.json').read_bytes()
    original_usage = (state.root / 'usage.jsonl').read_bytes()
    cache = {p.name: p.read_bytes() for p in (state.root / 'response-cache').glob('*')}
    before_db = state.settings.db_path.read_bytes()
    assert run(state)['status'] == 'MANUAL_DRAFT_READY'
    assert len(state.paid) == 1
    params = state.paid[0]
    assert params['text']['format']['name'] == 'reviewer'
    assert params['max_output_tokens'] == 6000 and params['model'] == 'gpt-5'
    assert params['service_tier'] == 'default'
    assert params['max_tool_calls'] == 3
    assert EXCERPT in params['input']
    assert all(item['max_retries'] == 0 for item in state.constructors)
    once = state.folder / 'review-once'
    assert (once / 'original-claim.json').read_bytes() == original_claim
    assert (state.folder / 'paid-calls.json').read_bytes() == original_calls
    assert (state.root / 'usage.jsonl').read_bytes() == original_usage
    assert {p.name: p.read_bytes() for p in (state.root / 'response-cache').glob('*')} == cache
    assert state.settings.db_path.read_bytes() == before_db
    result = json.loads((state.folder / 'result.json').read_text())
    for key, value in state.draft.items():
        assert result['post'][key] == value
    assert result['approval']['status'] == 'HELD'
    assert result['review_recovery']['approval_reference'] == state.packet['approval_reference']
    saved = [json.loads(p.read_text()) for p in (once / 'response-cache').glob('*.json')
             if 'context' in json.loads(p.read_text())]
    assert any(s['context']['provenance']['reference_evidence'][0]['text']
               == state.packet['evidence'][0]['text'] for s in saved)
    assert run(state)['cached'] is True
    assert len(state.paid) == 1


def test_original_held_request_and_old_cache_do_not_receive_retroactive_pass(state):
    from blogbot.inputs import ContentRequest

    request = ContentRequest(REQUEST_ID, 'origins', {'question': state.original['question']},
                             provenance={'reference_evidence': state.packet['evidence']})
    old = json.loads((state.root / 'response-cache' / f'{DAY}-3.json').read_text())
    assert _checked_review(old['payload'], old['response'],
                           _cached_review_request(request, old))['decision'] == 'REWRITE'
    token, pin = state.encode()
    manual_review.validate_review_packet(state.settings, token, REQUEST_ID, pin)
    assert json.loads((state.folder / 'claim.json').read_text())['status'] == 'HELD'
    assert not (state.folder / 'result.json').exists()
    assert state.paid == []


@pytest.mark.parametrize('field,value', [
    ('scope', 'draft_only'), ('date', '2026-10-10'), ('request_id', 'other-request'),
    ('approval_reference', 'Sentinel_test_original_article_approval'),
    ('approval_reference', 'Sentinel_test_original_estimate_consent'),
    ('approval_reference', ''), ('original_approval_reference', 'Sentinel_different_original'),
    ('original_claim_sha256', '0' * 64), ('original_calls_sha256', '0' * 64),
    ('writer_payload_sha256', '0' * 64), ('writer_response_id', 'original-writer'),
    ('cache_file', '../escape.json'), ('cache_file', '2026-10-11-0.json'),
    ('question', '변경된 질문'), ('approved_by', 'source_page'),
])
def test_wrong_approval_identity_or_manuscript_pin_cannot_reserve(state, field, value):
    packet = copy.deepcopy(state.packet)
    packet[field] = value
    with pytest.raises(ValueError):
        run(state, packet)
    assert state.paid == state.remote == []
    assert not (state.folder / 'review-once').exists()


@pytest.mark.parametrize('field,value', [
    ('url', 'https://korean.go.kr.evil.test/source'), ('url', 'https://example.test/source'),
    ('url', SOURCE + '&different_document=1'), ('text', ''), ('sha256', '0' * 64),
    ('verified_by', 'source_page'), ('retrieved_at', '2026-10-11T11:00:00'),
    ('retrieved_at', '2026-10-10T11:00:00+09:00'),
    ('retrieved_at', '2026-10-11T13:00:00+09:00'),
])
def test_new_review_evidence_requires_original_official_source_and_current_read(state, field, value):
    packet = copy.deepcopy(state.packet)
    packet['evidence'][0][field] = value
    with pytest.raises(ValueError):
        run(state, packet)
    assert state.paid == state.remote == []


def test_validly_hashed_other_excerpt_is_not_original_evidence(state):
    packet = copy.deepcopy(state.packet)
    packet['evidence'][0]['text'] = '다른 항목을 설명하는 글입니다.'
    packet['evidence'][0]['sha256'] = hashlib.sha256(packet['evidence'][0]['text'].encode()).hexdigest()
    with pytest.raises(ValueError, match='original exact source and excerpt'):
        run(state, packet)
    assert state.paid == []


@pytest.mark.parametrize('change', ['missing_call', 'uncertain_call', 'wrong_order', 'wrong_cache_id',
                                  'noncompleted_rewrite', 'edited_body', 'prior_drop', 'prior_unsafe'])
def test_only_exact_four_returned_calls_and_source_gate_failure_are_eligible(state, change):
    packet = copy.deepcopy(state.packet)
    if change in {'missing_call', 'uncertain_call'}:
        calls = copy.deepcopy(state.original_calls)
        if change == 'missing_call':
            calls.pop()
        else:
            calls[-1]['status'] = 'STARTED'
        path = state.folder / 'paid-calls.json'
        path.write_text(json.dumps(calls))
        packet['original_calls_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif change == 'wrong_order':
        path = state.root / 'usage.jsonl'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[1]['stage'] = 'rewrite'
        path.write_text('\n'.join(json.dumps(row) for row in rows))
    else:
        path = state.root / 'response-cache' / (f'{DAY}-3.json' if change.startswith('prior_')
                                               else f'{DAY}-2.json')
        cached = json.loads(path.read_text())
        if change == 'wrong_cache_id':
            cached['response']['id'] = 'other-response'
        elif change == 'noncompleted_rewrite':
            cached['response']['status'] = 'incomplete'
        elif change == 'edited_body':
            cached['payload']['body'] += '\n추가 문장'
        elif change == 'prior_drop':
            cached['payload']['decision'] = 'DROP'
        else:
            cached['payload']['scores'] = [3, 5, 5, 5, 5, 5]
            cached['payload']['total'] = 28
        path.write_text(json.dumps(cached))
    with pytest.raises(ValueError):
        run(state, packet)
    assert state.paid == []


@pytest.mark.parametrize('failure', ['timeout', 'truncation', 'drop', 'unsupported_source'])
def test_failed_extra_review_is_consumed_once_without_rewrite_or_paid_retry(state, failure):
    if failure == 'timeout':
        state.outcomes.append(TimeoutError('Unknown review outcome'))
    elif failure == 'truncation':
        state.outcomes.append(SimpleNamespace(id='truncated', model='gpt-5', status='incomplete',
                                             output=[], output_text='', usage=None,
                                             incomplete_details={'reason': 'max_output_tokens'}))
    elif failure == 'drop':
        state.outcomes.append(review(decision='DROP', scores=[2] * 6, total=12))
    else:
        state.outcomes.append(review(source_checks=[{'claim': '뜻', 'source_url': 'https://other.test/',
                                                     'evidence': 'unsupported', 'status': 'SUPPORTED'}]))
    with pytest.raises((ValueError, TimeoutError, RuntimeError)):
        run(state)
    assert len(state.paid) == 1
    original = (state.folder / 'paid-calls.json').read_bytes()
    assert run(state)['status'] == 'MANUAL_CHECK_REQUIRED'
    assert len(state.paid) == 1
    assert json.loads((state.folder / 'claim.json').read_text())['status'] == 'HELD'
    assert (state.folder / 'paid-calls.json').read_bytes() == original


def test_no_reservation_cannot_buy_a_review(state):
    token, pin = state.encode()
    with pytest.raises(ValueError, match='reservation is required'):
        manual_review.run_review(state.settings, token, REQUEST_ID, pin)
    assert state.paid == []


def test_missing_remote_reservation_cannot_buy_a_review(state, monkeypatch):
    token, pin = state.encode()
    manual_review.reserve_review(state.settings, token, REQUEST_ID, pin)
    monkeypatch.setattr(cloud, 'github_get', lambda path: b'{"artifacts": []}')
    with pytest.raises(ValueError, match='upload is not verified'):
        manual_review.run_review(state.settings, token, REQUEST_ID, pin)
    assert state.paid == []


def test_global_manual_lock_blocks_review_reservation(state):
    with media_claim(state.root, 'manual-requests'), pytest.raises(MediaBusy):
        run(state)
    assert state.paid == []


@pytest.mark.parametrize('change', ['other_run', 'rerun'])
def test_fresh_or_restarted_workflow_cannot_consume_old_review_slot(state, monkeypatch, change):
    token, pin = state.encode()
    manual_review.reserve_review(state.settings, token, REQUEST_ID, pin)
    if change == 'other_run':
        monkeypatch.setenv('GITHUB_RUN_ID', '456')
        assert manual_review.run_review(state.settings, token, REQUEST_ID, pin)['status'] == 'MANUAL_CHECK_REQUIRED'
    else:
        monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '2')
        with pytest.raises(ValueError, match='first-attempt owner workflow'):
            manual_review.run_review(state.settings, token, REQUEST_ID, pin)
    assert state.paid == []


def test_prior_spend_cannot_be_erased_to_fund_extra_review(state):
    calls = copy.deepcopy(state.original_calls)
    for call in calls:
        call['accounted_cost_usd'] = '0.7'
    path = state.folder / 'paid-calls.json'
    path.write_text(json.dumps(calls))
    packet = copy.deepcopy(state.packet)
    packet['original_calls_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='remaining approved estimated budget'):
        run(state, packet)
    assert state.paid == []


@pytest.mark.parametrize('invalid', ['plaintext', 'wrong_key', 'wrong_pin'])
def test_review_envelope_requires_authenticated_exact_packet(state, invalid):
    token, pin = state.encode()
    if invalid == 'plaintext':
        token = json.dumps(state.packet)
    elif invalid == 'wrong_key':
        token = Fernet(Fernet.generate_key()).encrypt(b'{}').decode()
    else:
        pin = '0' * 64
    with pytest.raises((ValueError, InvalidToken)):
        manual_review.reserve_review(state.settings, token, REQUEST_ID, pin)
    assert state.paid == []


def test_every_original_source_requires_explicit_current_excerpt_before_spend(state):
    path = state.folder / 'approved-packet.json'
    original = json.loads(path.read_text())
    original['sources'].append({'url': 'https://www.korean.go.kr/front/source/456',
                                'excerpt': '두 번째 사전 근거입니다.'})
    path.write_text(json.dumps(original))
    with pytest.raises(ValueError, match='every original source'):
        run(state)
    assert state.paid == []
    assert not (state.folder / 'review-once').exists()


def test_final_rewrite_reuses_original_historical_observed_urls_without_inventing_sources(state):
    path = state.root / 'response-cache' / f'{DAY}-2.json'
    cached = json.loads(path.read_text())
    cached['response']['output'] = []
    path.write_text(json.dumps(cached))
    assert run(state)['status'] == 'MANUAL_DRAFT_READY'
    assert len(state.paid) == 1
    assert json.loads((state.folder / 'result.json').read_text())['post']['source_urls'] == [SOURCE]


def test_review_reservation_and_completed_evidence_survive_encrypted_transport(state, tmp_path):
    token, pin = state.encode()
    manual_review.reserve_review(state.settings, token, REQUEST_ID, pin)
    reserved = tmp_path / 'reserved' / 'bundle.enc'
    cloud.pack(state.settings, reserved)
    with zipfile.ZipFile(io.BytesIO(Fernet(state.key).decrypt(reserved.read_bytes()))) as archive:
        base = f'manual-requests/{REQUEST_ID}/review-once/'
        assert json.loads(archive.read(base + 'claim.json'))['status'] == 'RESERVED'
        assert hashlib.sha256(archive.read(base + 'original-claim.json')).hexdigest() == (
            state.packet['original_claim_sha256'])
    restored_reservation = tmp_path / 'reserved-roundtrip'
    cloud.extract_bundle(reserved.read_bytes(), restored_reservation, state.key.decode())
    reserved_settings = replace(state.settings, db_path=restored_reservation / 'blog.db',
                                artifact_dir=restored_reservation / 'drafts',
                                inbox_dir=restored_reservation / 'inbox')
    assert manual_review.validate_review_packet(reserved_settings, token, REQUEST_ID, pin) == state.packet
    assert state.paid == []
    assert manual_review.run_review(state.settings, token, REQUEST_ID, pin)['status'] == 'MANUAL_DRAFT_READY'
    completed = tmp_path / 'completed' / 'bundle.enc'
    cloud.pack(state.settings, completed)
    with zipfile.ZipFile(io.BytesIO(Fernet(state.key).decrypt(completed.read_bytes()))) as archive:
        assert base + 'paid-calls.json' in archive.namelist()
        assert base + 'usage.jsonl' in archive.namelist()
        assert base + 'result.json' in archive.namelist()
        assert any(name.startswith(base + 'response-cache/') for name in archive.namelist())
        assert json.loads(archive.read('ready.json'))['posts'] == []
    restored = tmp_path / 'restored'
    cloud.extract_bundle(completed.read_bytes(), restored, state.key.decode())
    settings = replace(state.settings, db_path=restored / 'blog.db',
                       artifact_dir=restored / 'drafts', inbox_dir=restored / 'inbox')
    assert manual_review.run_review(settings, token, REQUEST_ID, pin)['cached'] is True
    nested = json.loads((restored / base / 'result.json').read_text())
    top = json.loads((restored / 'manual-requests' / REQUEST_ID / 'result.json').read_text())
    assert manual_requests._result_digest(nested) == manual_requests._result_digest(top)
    assert Path(nested['post']['photos'][0]['file']).is_relative_to(restored)
    assert Path(nested['input']['photos'][0]['file']).is_relative_to(restored)
    assert len(state.paid) == 1


def test_older_bundle_cannot_remove_a_locally_reserved_review_slot(state, tmp_path):
    older = tmp_path / 'old' / 'bundle.enc'
    cloud.pack(state.settings, older)
    token, pin = state.encode()
    manual_review.reserve_review(state.settings, token, REQUEST_ID, pin)
    original = (state.folder / 'review-once' / 'claim.json').read_bytes()
    with pytest.raises(ValueError, match='checkpoint conflict'):
        cloud.extract_bundle(older.read_bytes(), state.root, state.key.decode())
    assert (state.folder / 'review-once' / 'claim.json').read_bytes() == original
    assert state.paid == []


@pytest.mark.parametrize('mode,expected,count', [
    ('manual-review-reserve', 'MANUAL_REVIEW_RESERVED', 0),
    ('manual-review', 'MANUAL_DRAFT_READY', 1),
])
def test_cloud_selected_review_skips_all_candidate_generation_and_saving(
        state, monkeypatch, tmp_path, capsys, mode, expected, count):
    token, pin = state.encode()
    if mode == 'manual-review':
        manual_review.reserve_review(state.settings, token, REQUEST_ID, pin)
    monkeypatch.setattr(cloud, 'load_settings', lambda: state.settings)
    monkeypatch.setattr(cloud, 'restore', lambda directory: None)
    for method in ['run_daily', 'seed_inputs', 'recover_preparation', 'import_manual_saves']:
        monkeypatch.setattr(cloud, method, lambda *a, **kw: pytest.fail('No ordinary pipeline'))
    output = tmp_path / 'cloud' / 'bundle.enc'
    monkeypatch.setenv('BLOG_MANUAL_REQUEST_PACKET', token)
    monkeypatch.setenv('BLOG_ENCRYPTED_OUTPUT', str(output))
    monkeypatch.setattr('sys.argv', ['cloud', mode, '--manual-request-id', REQUEST_ID,
                                    '--manual-request-sha256', pin])
    cloud.main()
    assert output.exists()
    assert len(state.paid) == count
    stdout = capsys.readouterr().out
    assert expected in stdout
    assert token not in stdout and EXCERPT not in stdout


@pytest.mark.parametrize('invalid', ['symlink', 'unknown_file', 'unknown_nested_file'])
def test_review_transport_rejects_symlinks_and_unmanaged_checkpoint_names(state, tmp_path, invalid):
    token, pin = state.encode()
    manual_review.reserve_review(state.settings, token, REQUEST_ID, pin)
    once = state.folder / 'review-once'
    if invalid == 'symlink':
        target = once / 'paid-calls.json'
        target.symlink_to(state.folder / 'paid-calls.json')
    elif invalid == 'unknown_file':
        (once / 'unexpected.json').write_text('{}')
    else:
        (once / 'response-cache').mkdir()
        (once / 'response-cache' / 'unexpected.txt').write_text('unmanaged')
    with pytest.raises(ValueError):
        cloud.pack(state.settings, tmp_path / 'rejected' / 'bundle.enc')
    assert state.paid == []
