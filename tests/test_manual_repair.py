"""Offline boundaries for the separately authorized parenting repair."""
import hashlib
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from blogbot import core, planning, pre_review
from blogbot import manual_repair as repair


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def case(tmp_path, monkeypatch):
    day = lambda: date(2026, 10, 11)
    for module in (repair, core, planning, pre_review):
        monkeypatch.setattr(module, 'today_kst', day)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('GITHUB_RUN_ID', '456')
    monkeypatch.setenv('GITHUB_RUN_ATTEMPT', '1')
    monkeypatch.setattr(repair, '_run_identity', lambda: '456')
    monkeypatch.setattr(repair, 'check_remote_history', lambda *a: None)
    monkeypatch.setattr(repair, 'verify_remote_reservation', lambda *a: None)
    plan = {'date': '2026-10-11', 'category': 'parenting', 'version': 'weekly-4111-v1'}
    settings = SimpleNamespace(db_path=tmp_path / 'blog.db', root=tmp_path,
        openai_api_key='not-real', openai_model='gpt-5', review_model='gpt-5',
        config={'daily_plan': plan, 'categories': {'parenting': {'subcategories': ['육아생활']}},
                'editorial': {'require_structure': True},
                'blog': {'max_similarity': .9, 'review_pass_score': 24},
                'images': {'model': 'gpt-image-2.5-flare', 'quality': 'medium',
                           'size': '1024x1024', 'output_format': 'jpeg', 'parenting_count': 5}})
    conn = core.connect_db(settings.db_path)
    conn.execute('INSERT INTO attempts(day,category,status,request_id,plan_json) VALUES (?,?,?,?,?)',
                 ('2026-10-11', 'parenting', 'DROP_REVIEW', repair.REQUEST_ID, json.dumps(plan)))
    conn.commit()
    conn.close()
    original = {'category': 'parenting', 'data': {'question': '아기와 어떻게 놀까요?'},
                'photos': [], 'provenance': {'daily_plan': plan}}
    folder = tmp_path / 'response-cache'
    folder.mkdir()
    key = hashlib.sha256(repair.REQUEST_ID.encode()).hexdigest()
    checkpoint = folder / f'2026-10-11-pre-review-{key}.json'
    repair.atomic_json(checkpoint, {'request': original, 'correction_attempted': True})
    repair.atomic_json(checkpoint.with_suffix('.revision.json'), {'stage': 'pre_review_correction'})
    writer_path = folder / ('2026-10-11-' + 'a' * 64 + '.json')
    old_payload = {'old': 'immutable'}
    repair.atomic_json(writer_path, {'payload': old_payload, 'response': {'id': 'writer', 'status': 'completed'}})
    review_path = folder / ('2026-10-11-' + 'b' * 64 + '.json')
    repair.atomic_json(review_path, {'payload': {'scores': [3, 4, 4, 4, 5, 5], 'total': 25,
            'decision': 'REWRITE'}, 'response': {'id': 'review', 'status': 'completed'}})
    history = []
    for i, stage in enumerate(['benchmark', 'writer', 'pre_review_correction', 'reviewer', 'reviewer']):
        history.append({'request_id': repair.REQUEST_ID, 'stage': stage,
            'status': 'incomplete' if i == 3 else 'completed', 'attempt': 2 if i == 4 else 1,
            'incomplete_reason': 'max_output_tokens' if i == 3 else None,
            'response_id': 'writer' if i == 2 else 'review' if i == 4 else f'r{i}',
            'model': 'gpt-5', 'estimated_text_usd': '0.4253085' if i == 0 else '0',
            'estimated_search_call_usd': 0})
    (tmp_path / 'usage.jsonl').write_text('\n'.join(json.dumps(c) for c in history))
    url = 'https://www.healthychildren.org/English/ages-stages/baby/Pages/example.aspx'
    payload = {'title': '아기와 편안하게 놀기', 'subcategory': '육아생활',
               'body': '깨어 있을 때 보호자와 함께 놉니다.\n\n## 얼굴 보며 놀이\n아기에게 말을 걸고 반응을 살핍니다.',
               'tags': ['육아', '놀이', '아기'], 'source_urls': [url], 'as_of_date': '2026-10-11'}
    text = 'Verified original source passage. ' * 12
    packet = {'version': repair.VERSION, 'request_id': repair.REQUEST_ID, 'date': '2026-10-11',
        'approval_reference': 'Sentinel_explicit_owner_approval', 'approved_by': 'repository_owner',
        'scope': 'draft_only', 'category': 'parenting', 'question': original['data']['question'],
        'original_usage_sha256': sha(tmp_path / 'usage.jsonl'),
        'original_request_sha256': repair._digest(original), 'original_plan_sha256': repair._digest(plan),
        'original_claim_sha256': sha(checkpoint.with_suffix('.revision.json')),
        'original_checkpoint_sha256': sha(checkpoint), 'writer_cache_file': writer_path.name,
        'writer_payload_sha256': repair._digest(old_payload), 'writer_response_id': 'writer',
        'original_review_cache_file': review_path.name, 'original_review_sha256': sha(review_path),
        'corrected_post': payload, 'corrected_post_sha256': repair._digest(payload),
        'evidence': [{'url': url, 'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
                      'retrieved_at': '2026-10-11T00:00:00+00:00', 'verified_by': 'operator',
                      'evidence_kind': 'selected_original_passages'}], 'budget': dict(repair.BUDGET)}
    key = Fernet.generate_key()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key.decode())

    def encrypt(value=packet):
        raw = json.dumps(value, ensure_ascii=False).encode()
        ciphertext = Fernet(key).encrypt(raw).decode()
        monkeypatch.setenv('BLOG_MANUAL_REPAIR_PACKET', ciphertext)
        return ciphertext, hashlib.sha256(raw).hexdigest()
    return settings, packet, encrypt


def test_valid_packet_preserves_original_state(case):
    settings, packet, encrypt = case
    before = {str(p): p.read_bytes() for p in settings.db_path.parent.rglob('*') if p.is_file()}
    ciphertext, digest = encrypt()
    assert repair.validate_repair_packet(settings, ciphertext, repair.REQUEST_ID, digest) == packet
    repair.reserve(settings, repair.REQUEST_ID, digest)
    assert all(Path(p).read_bytes() == data for p, data in before.items())


@pytest.mark.parametrize('field,value', [
    ('approval_reference', ''), ('scope', 'publish'), ('category', 'origins'),
    ('request_id', 'owner-new-identity'), ('date', '2026-10-12'),
    ('original_claim_sha256', 'f' * 64), ('original_usage_sha256', 'f' * 64),
    ('original_review_sha256', 'f' * 64), ('writer_payload_sha256', 'f' * 64),
    ('corrected_post_sha256', 'f' * 64), ('original_request_sha256', 'f' * 64),
])
def test_approval_scope_identity_and_pins_fail_closed(case, field, value):
    settings, packet, encrypt = case
    packet[field] = value
    ciphertext, digest = encrypt()
    with pytest.raises(ValueError):
        repair.validate_repair_packet(settings, ciphertext, repair.REQUEST_ID, digest)


@pytest.mark.parametrize('change', ['hash', 'summary', 'host', 'uncovered', 'time'])
def test_evidence_is_exact_original_attributed_and_complete(case, change):
    settings, packet, encrypt = case
    evidence = packet['evidence'][0]
    if change == 'hash':
        evidence['text'] += ' changed'
    elif change == 'summary':
        evidence['evidence_kind'] = 'operator_summary_not_verbatim'
    elif change == 'host':
        evidence['url'] = 'https://www.cdc.gov.attacker.example/article'
    elif change == 'uncovered':
        packet['corrected_post']['source_urls'].append('https://www.cdc.gov/other')
        packet['corrected_post_sha256'] = repair._digest(packet['corrected_post'])
    else:
        evidence['retrieved_at'] = '2026-10-10T00:00:00+00:00'
    ciphertext, digest = encrypt()
    with pytest.raises(ValueError):
        repair.validate_repair_packet(settings, ciphertext, repair.REQUEST_ID, digest)


@pytest.mark.parametrize('key,value', [('max_images', 6), ('review_calls', 2),
    ('max_estimated_usd', '2.0'), ('sdk_retries', 1), ('truncation_retries', 1),
    ('image_max_attempts', 2), ('image_prompt_max_bytes', 20001)])
def test_budget_expansion_rejected(case, key, value):
    settings, packet, encrypt = case
    packet['budget'][key] = value
    ciphertext, digest = encrypt()
    with pytest.raises(ValueError):
        repair.validate_repair_packet(settings, ciphertext, repair.REQUEST_ID, digest)


def test_paid_ledger_limits_and_durable_preclaim(case, tmp_path):
    ledger = repair.PaidLedger(tmp_path)
    ledger.claim('review', {'model': 'gpt-5'})
    assert json.loads(ledger.path.read_text())[0]['status'] == 'CLAIMED'
    with pytest.raises(ValueError):
        ledger.claim('review', {})
    for index in range(5):
        ledger.claim('image', {'model': repair.BUDGET['image_model'], 'prompt': str(index),
                               'quality': 'medium', 'size': '1024x1024', 'output_format': 'jpeg', 'n': 1})
    with pytest.raises(ValueError):
        ledger.claim('image', {})
    assert len(json.loads(ledger.path.read_text())) == 6


def fake_review(monkeypatch, packet, *, passed=True, exception=False):
    class Fake:
        def __init__(self, *args):
            self.client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs:
                SimpleNamespace(usage=None)))

        def review(self, post, info, request, *, single_attempt):
            assert single_attempt is True
            assert request.provenance['reference_evidence'][0]['verified_by'] == 'operator'
            self.client.responses.create(model='gpt-5', max_output_tokens=6000)
            if exception:
                raise TimeoutError('uncertain')
            return {'scores': [5] * 6 if passed else [3, 4, 4, 4, 5, 5],
                    'total': 30 if passed else 25, 'decision': 'PASS' if passed else 'REWRITE',
                    'source_checks': [{'source_url': packet['evidence'][0]['url'],
                                       'status': 'SUPPORTED', 'evidence': 'Actual passage supports claim'}]}
    monkeypatch.setattr(repair, 'BlogLLM', Fake)


@pytest.mark.parametrize('exception', [False, True])
def test_failed_or_uncertain_review_never_images_or_retries(case, monkeypatch, exception):
    settings, packet, encrypt = case
    _, digest = encrypt()
    fake_review(monkeypatch, packet, passed=False, exception=exception)
    monkeypatch.setattr(repair, 'execute_image_plans', lambda *a, **k: pytest.fail('No images on FAIL'))
    repair.reserve(settings, repair.REQUEST_ID, digest)
    with pytest.raises((ValueError, TimeoutError)):
        repair.prepare(settings, repair.REQUEST_ID, digest)
    folder = repair._folder(settings)
    assert len(json.loads((folder / 'paid-calls.json').read_text())) == 1
    assert json.loads((folder / 'claim.json').read_text())['status'] == repair.HELD
    with pytest.raises(ValueError):
        repair.prepare(settings, repair.REQUEST_ID, digest)
    with pytest.raises(ValueError):
        repair.reserve(settings, repair.REQUEST_ID, digest)


def test_complete_result_replays_without_charge_and_originals_immutable(case, monkeypatch):
    settings, packet, encrypt = case
    original = {p: p.read_bytes() for p in settings.db_path.parent.rglob('*') if p.is_file()}
    _, digest = encrypt()
    fake_review(monkeypatch, packet)

    def images(settings, folder, plans, placements, **kwargs):
        assert kwargs['allow_uncertain_recovery'] is False and kwargs['max_attempts'] == 1
        folder.mkdir()
        result = []
        for params, key in plans:
            assert params['n'] == 1 and params['model'] == 'gpt-image-2.5-flare'
            kwargs['before_submit'](params)
            path = folder / f'{key}.jpg'
            path.write_bytes(b'test-image')
            result.append({'file': str(path), 'sha256': sha(path), 'generated': True})
        return result
    monkeypatch.setattr(repair, 'execute_image_plans', images)
    repair.reserve(settings, repair.REQUEST_ID, digest)
    result = repair.prepare(settings, repair.REQUEST_ID, digest)
    assert result['images'] == 2
    assert result['status'] == repair.READY
    assert all(p.read_bytes() == data for p, data in original.items())
    monkeypatch.setattr(repair, 'BlogLLM', lambda *a: pytest.fail('Cached result must not charge'))
    assert repair.prepare(settings, repair.REQUEST_ID, digest)['cached'] is True
    assert repair.reserve(settings, repair.REQUEST_ID, digest)['cached'] is True
    folder = repair._folder(settings)
    assert len(json.loads((folder / 'paid-calls.json').read_text())) == 3
    assert json.loads((folder / 'result.json').read_text())['costs']['original_estimated_usd'] == '0.4253085'


def test_remote_reservation_required_before_any_paid_work(case, monkeypatch):
    settings, _, encrypt = case
    _, digest = encrypt()
    repair.reserve(settings, repair.REQUEST_ID, digest)
    def missing(*args):
        raise ValueError('remote missing')
    monkeypatch.setattr(repair, 'verify_remote_reservation', missing)
    monkeypatch.setattr(repair, 'BlogLLM', lambda *a: pytest.fail('No paid client before remote claim'))
    with pytest.raises(ValueError, match='remote missing'):
        repair.prepare(settings, repair.REQUEST_ID, digest)
    assert not (repair._folder(settings) / 'paid-calls.json').exists()


def test_image_plan_count_and_prompt_byte_cap(case, monkeypatch):
    settings, packet, _ = case
    state = repair._original_state(settings, packet)
    state['post'].body += '\n\n' + '\n\n'.join(f'## 장면{i}\n설명{i}' for i in range(8))
    plans, placements = repair._image_plans(settings, state['request'], state['post'])
    assert len(plans) == len(placements) == 5
    monkeypatch.setattr(repair, 'image_prompt', lambda *a, **k: '가' * 6667)
    with pytest.raises(ValueError, match='20000-byte'):
        repair._image_plans(settings, state['request'], state['post'])


def test_actual_routed_review_receives_exact_operator_originals(case, monkeypatch):
    from blogbot import llm, research
    settings, packet, _ = case
    state = repair._original_state(settings, packet)
    folder = repair._folder(settings)
    sources = folder / 'source-evidence'
    sources.mkdir(parents=True)
    for evidence in state['evidence']:
        path = sources / ('reference-' + hashlib.sha256(evidence['url'].encode()).hexdigest() + '.json')
        repair.atomic_json(path, evidence)
    monkeypatch.setattr(research, 'today_kst', lambda: date(2026, 10, 11))
    monkeypatch.setattr(research, 'urlopen', lambda *a, **k: pytest.fail('Pinned excerpts need no fetch'))
    calls = []
    def response(*args, **kwargs):
        calls.append(kwargs)
        return {'decision': 'PASS', 'source_checks': [{'source_url': packet['evidence'][0]['url'],
            'status': 'SUPPORTED', 'evidence': 'Verified original source passage.'}]}, {'output': []}
    monkeypatch.setattr(llm, 'request_json', response)
    client = llm.BlogLLM.__new__(llm.BlogLLM)
    client.client = None
    client.journal = folder / 'usage.jsonl'
    client.model = client.review_model = 'gpt-5'
    client.reviewer_prompt = (Path(__file__).resolve().parents[1] / 'prompts/reviewer.md').read_text()
    result = client.review(state['post'], {'rules': []}, state['request'], single_attempt=True)
    assert result['decision'] == 'PASS'
    actual = calls[0]['cache_context']['provenance']['reference_evidence']
    assert actual == state['evidence']
    assert actual[0]['text'] == packet['evidence'][0]['text']
    assert actual[0]['verified_by'] == 'operator'
    assert actual[0]['evidence_kind'] == 'selected_original_passages'
    assert actual[0]['sha256'] in calls[0]['input']
    assert 'not model-opened full pages' in calls[0]['input']
    assert calls[0]['max_output_tokens'] == 6000
    assert calls[0]['retry_output_tokens'] is None
    assert calls[0]['max_tool_calls'] == 3


def test_real_review_cost_reduces_image_allowance(case):
    settings, _, _ = case
    folder = settings.db_path.parent
    ledger = repair.PaidLedger(folder)
    ledger.claim('review', {'model': 'gpt-5'})
    repair.atomic_json(folder / 'review-calls.json', [{'accounted_cost_usd': '1.45'}])
    params = {'model': repair.BUDGET['image_model'], 'prompt': 'test', 'quality': 'medium',
              'size': '1024x1024', 'output_format': 'jpeg', 'n': 1}
    with pytest.raises(ValueError, match='budget'):
        ledger.claim('image', params)
    assert len(json.loads(ledger.path.read_text())) == 1


def test_image_usage_unknown_keeps_reservation_and_reported_usage_increases_it(case):
    settings, _, _ = case
    ledger = repair.PaidLedger(settings.db_path.parent)
    ledger.claim('review', {})
    params = {'model': repair.BUDGET['image_model'], 'prompt': 'test', 'quality': 'medium',
              'size': '1024x1024', 'output_format': 'jpeg', 'n': 1}
    ledger.claim('image', params)
    assert str(ledger.accounted()) == '0.71317'
    call = json.loads(ledger.path.read_text())[-1]
    folder = settings.db_path.parent / 'images'
    folder.mkdir()
    path = folder / (call['image_cache_key'] + '.json')
    repair.atomic_json(path, {'usage': None})
    assert str(ledger.accounted()) == '0.71317'
    repair.atomic_json(path, {'usage': {'input_tokens': 100000, 'output_tokens': 20000}})
    assert ledger.accounted() > 1.5
    with pytest.raises(ValueError, match='budget'):
        ledger.claim('image', params)


@pytest.mark.parametrize('problem', ['low_score', 'missing_source_check'])
def test_config_cannot_lower_review_gate_or_skip_source_coverage(case, monkeypatch, problem):
    settings, packet, encrypt = case
    _, digest = encrypt()
    settings.config['blog']['review_pass_score'] = 20
    fake_review(monkeypatch, packet)
    old_review = repair.BlogLLM.review
    def review(self, *args, **kwargs):
        result = old_review(self, *args, **kwargs)
        if problem == 'low_score':
            result.update(scores=[4, 4, 3, 3, 4, 4], total=22)
        else:
            result['source_checks'] = []
        return result
    monkeypatch.setattr(repair.BlogLLM, 'review', review)
    monkeypatch.setattr(repair, 'execute_image_plans', lambda *a, **k: pytest.fail('No images'))
    repair.reserve(settings, repair.REQUEST_ID, digest)
    with pytest.raises(ValueError, match='review failed'):
        repair.prepare(settings, repair.REQUEST_ID, digest)


def test_uncertain_claim_cannot_be_reopened_by_a_new_workflow_run(case, monkeypatch):
    settings, _, encrypt = case
    _, digest = encrypt()
    repair.reserve(settings, repair.REQUEST_ID, digest)
    monkeypatch.setattr(repair, '_run_identity', lambda: '999')
    with pytest.raises(ValueError, match='no retry'):
        repair.prepare(settings, repair.REQUEST_ID, digest)
    with pytest.raises(ValueError, match='unresolved'):
        repair.reserve(settings, repair.REQUEST_ID, digest)


def test_completed_result_tampering_and_approval_replacement_rejected(case, monkeypatch):
    settings, packet, encrypt = case
    _, digest = encrypt()
    repair.reserve(settings, repair.REQUEST_ID, digest)
    folder = repair._folder(settings)
    claim = json.loads((folder / 'claim.json').read_text())
    claim.update(status=repair.READY, result_sha256='f' * 64)
    repair.atomic_json(folder / 'claim.json', claim)
    repair.atomic_json(folder / 'result.json', {'post': {'photos': []}, 'input': {'photos': []}})
    with pytest.raises(ValueError, match='integrity'):
        repair.prepare(settings, repair.REQUEST_ID, digest)
    packet['approval_reference'] = 'Sentinel_changed_approval'
    _, changed = encrypt()
    with pytest.raises(ValueError, match='cannot be replaced'):
        repair.reserve(settings, repair.REQUEST_ID, changed)


def test_uncertain_image_preserves_paid_claim_and_never_resubmits(case, monkeypatch):
    from blogbot.images import ImagePending
    settings, packet, encrypt = case
    _, digest = encrypt()
    fake_review(monkeypatch, packet)
    def images(settings, folder, plans, placements, **kwargs):
        kwargs['before_submit'](plans[0][0])
        raise ImagePending('response uncertain')
    monkeypatch.setattr(repair, 'execute_image_plans', images)
    repair.reserve(settings, repair.REQUEST_ID, digest)
    with pytest.raises(ImagePending):
        repair.prepare(settings, repair.REQUEST_ID, digest)
    assert len(json.loads((repair._folder(settings) / 'paid-calls.json').read_text())) == 2
    with pytest.raises(ValueError):
        repair.prepare(settings, repair.REQUEST_ID, digest)


def test_original_state_change_after_review_blocks_images(case, monkeypatch):
    settings, packet, encrypt = case
    _, digest = encrypt()
    fake_review(monkeypatch, packet)
    old_review = repair.BlogLLM.review
    def review(self, *args, **kwargs):
        result = old_review(self, *args, **kwargs)
        with (settings.db_path.parent / 'usage.jsonl').open('a') as stream:
            stream.write('\n{}')
        return result
    monkeypatch.setattr(repair.BlogLLM, 'review', review)
    def images(settings, folder, plans, placements, **kwargs):
        kwargs['before_submit'](plans[0][0])
        pytest.fail('Changed original state must block submission')
    monkeypatch.setattr(repair, 'execute_image_plans', images)
    repair.reserve(settings, repair.REQUEST_ID, digest)
    with pytest.raises(ValueError, match='usage history changed'):
        repair.prepare(settings, repair.REQUEST_ID, digest)
    assert len(json.loads((repair._folder(settings) / 'paid-calls.json').read_text())) == 1
