"""A dated operator correction can authorize one isolated review, never a rewrite."""
import hashlib
import json
import os
from dataclasses import asdict, replace
from types import SimpleNamespace as NS

import pytest
from cryptography.fernet import Fernet

from blogbot.core import connect_db, save_post, today_kst
from blogbot.images import atomic_json
from blogbot.inputs import ContentRequest
from blogbot.llm import BlogLLM
from blogbot.planning import media_claim
from blogbot.pre_review import (
    DraftCandidate,
    checkpoint_path,
    claim_revision,
    inspect_candidate,
    revision_path,
    run_pre_review,
)
from blogbot.recovery import (
    _attribution_correction,
    attribution_review_hashes,
    recover_attribution_review,
    recover_rejected,
    source_review_hashes,
)
from blogbot.responses import ResponseFailure

SOURCE = 'https://www.nsca.com/education/articles/kinetic-select/face-pull-machine/'
INFO = {'display_name': '운동', 'subcategories': ['등'], 'max_daily': 1, 'rules': []}


def review(*, pass_review=True):
    return {'scores': [5, 5, 4, 5, 5, 4], 'total': 28,
            'decision': 'PASS' if pass_review else 'REWRITE', 'issues': [],
            'blocking_issues': [] if pass_review else ['사실 확인 미완료'],
            'rewrite_instructions': '', 'source_checks': [
                {'claim': '운동 자세', 'source_url': SOURCE, 'evidence': '직접 읽은 본문',
                 'status': 'SUPPORTED'}]}


@pytest.fixture
def state(tmp_path, monkeypatch, weekday_clock):
    request = ContentRequest('exercise-source-review', 'exercise', {'question': '로프 당기기 자세'},
                             provenance={'benchmark': {'records': [], 'limitations': 'fixture'}})
    body = '가벼운 저항으로 자세를 확인합니다.\n\n' + '\n\n'.join(
        f'## 단계 {i}\n\n' + ('원문으로 확인한 동작을 천천히 수행합니다. ' * 28)
        for i in range(4)) + '\n\n### 중단 신호\n\n불편하면 멈춥니다.'
    raw = DraftCandidate({'title': '로프 당기기 동작과 안전한 자세', 'subcategory': '등',
                          'body': body, 'tags': ['운동'], 'source_urls': [SOURCE],
                          'as_of_date': str(today_kst())}, [SOURCE])
    post, _ = run_pre_review(tmp_path, NS(), raw, request, INFO)
    claim_revision(tmp_path, request.id, 'rewrite', {'already_used': True})
    folder = tmp_path / 'response-cache'
    for stage, payload in [('writer', raw.payload), ('rewrite', raw.payload), ('reviewer', review())]:
        atomic_json(folder / f'{today_kst()}-{stage}.json', {
            'payload': payload, 'response': {'id': stage, 'output': [{
                'status': 'completed', 'action': {'type': 'search', 'sources': [{'url': SOURCE}]}}]}})
    (tmp_path / 'usage.jsonl').write_text('\n'.join(json.dumps({
        'request_id': request.id, 'stage': stage, 'response_id': stage})
        for stage in ['writer', 'rewrite', 'reviewer']))
    recovery_state = {'date': str(today_kst()), 'attempted': True, 'attempts': 2}
    atomic_json(tmp_path / 'auto-recovery.json', recovery_state)
    settings = NS(root=tmp_path / 'root', db_path=tmp_path / 'blog.db',
                  artifact_dir=tmp_path / 'drafts', openai_api_key='offline-only',
                  openai_model='unchanged-writer', review_model='unchanged-reviewer',
                  config={'categories': {'exercise': INFO}, 'blog': {
                      'review_pass_score': 24, 'max_similarity': .9, 'daily_max': 1},
                      'editorial': {'require_structure': True}})
    settings.artifact_dir.mkdir()
    with connect_db(settings.db_path) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), request.category, request.id, 'DROP_REVIEW'))
    key = Fernet.generate_key()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key.decode())
    patch_path = settings.root / 'editorial' / str(today_kst()) / (
        hashlib.sha256(request.id.encode()).hexdigest() + '.enc')
    patch_path.parent.mkdir(parents=True)
    patch = {'request_id': request.id, 'as_of_date': str(today_kst()),
             'body': post.body.replace('가벼운 저항', '통제 가능한 저항'),
             'review_recovery': source_review_hashes(post, request)}
    def save_patch(value=patch):
        patch_path.write_bytes(Fernet(key).encrypt(json.dumps(value, ensure_ascii=False).encode()))
    calls, media = [], []
    class Reviewer:
        def __init__(self, *args):
            assert args[1] == 'unchanged-writer' and args[3] == 'unchanged-reviewer'
        def rewrite(self, *args, **kwargs): pytest.fail('Never another paid manuscript correction')
        def create_draft(self, *args, **kwargs): pytest.fail('Never a replacement writer')
        def review(self, post, info, request, **kwargs):
            calls.append((post, request, kwargs))
            return review()
    monkeypatch.setattr('blogbot.recovery.BlogLLM', Reviewer)
    monkeypatch.setattr('blogbot.recovery.prepare_request', lambda *args: pytest.fail('No benchmark'))
    def complete(settings, conn, post_id, post, request, checked):
        media.append((post_id, post.request_id, checked))
        return {'status': 'APPROVED', 'post_id': post_id}
    monkeypatch.setattr('blogbot.pipeline.complete_media', complete)
    return NS(settings=settings, request=request, post=post, raw=raw, patch=patch,
              patch_path=patch_path, save_patch=save_patch, calls=calls, media=media,
              recovery_state=recovery_state)


def recover(state, request=None):
    with connect_db(state.settings.db_path) as conn:
        return recover_rejected(state.settings, conn, [request or state.request])


def test_missing_explicit_manifest_keeps_original_correction_guard(state):
    assert recover(state)[0]['reason'] == 'manuscript_correction_limit'
    state.save_patch({k: v for k, v in state.patch.items() if k != 'review_recovery'})
    assert recover(state)[0]['reason'] == 'manuscript_correction_limit'
    assert state.calls == state.media == []


def test_authorized_source_review_keeps_identity_budget_and_media(state):
    directory = state.settings.db_path.parent
    unchanged = {p.name: p.read_bytes() for p in [
        revision_path(directory, state.request.id), directory / 'auto-recovery.json',
        directory / 'usage.jsonl', directory / 'response-cache' / f'{today_kst()}-reviewer.json']}
    state.save_patch()
    result, = recover(state)
    assert result['status'] == 'APPROVED' and result['request_id'] == state.request.id
    assert len(state.calls) == len(state.media) == 1
    post, request, options = state.calls[0]
    assert post.request_id == request.id == state.request.id
    assert options == {'cache_only': False, 'single_attempt': True}
    assert post.body.startswith('통제 가능한 저항')
    assert recover(state) == []
    with connect_db(state.settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 1
    for name, before in unchanged.items():
        path = directory / name if name in {'auto-recovery.json', 'usage.jsonl'} else directory / 'response-cache' / name
        assert path.read_bytes() == before


@pytest.mark.parametrize('change', ['request_hash', 'manuscript_hash', 'version', 'id', 'date', 'source'])
def test_wrong_scope_or_new_sources_cannot_buy_review(state, change):
    patch = json.loads(json.dumps(state.patch))
    if change == 'request_hash': patch['review_recovery']['request_sha256'] = 'wrong'
    if change == 'manuscript_hash': patch['review_recovery']['original_manuscript_sha256'] = 'wrong'
    if change == 'version': patch['review_recovery']['version'] = 'future'
    if change == 'id': patch['request_id'] = 'other'
    if change == 'date': patch['as_of_date'] = '2026-09-29'
    if change == 'source': patch['source_urls'] = ['https://unobserved.example/source']
    state.save_patch(patch)
    assert recover(state)[0]['status'] == 'ERROR'
    assert state.calls == state.media == []


def test_changed_original_request_cannot_buy_review(state):
    state.save_patch()
    changed = replace(state.request, data={'question': '다른 질문'})
    assert recover(state, changed)[0]['status'] == 'ERROR'
    assert state.calls == []


@pytest.mark.parametrize('decision', ['REWRITE', 'DROP', 'unsafe', 'blocking'])
def test_only_source_reading_failure_is_eligible(state, decision):
    path = state.settings.db_path.parent / 'response-cache' / f'{today_kst()}-reviewer.json'
    cached = json.loads(path.read_text())
    if decision == 'unsafe':
        cached['payload']['scores'] = [3, 5, 5, 5, 5, 5]
    elif decision == 'blocking': cached['payload']['blocking_issues'] = ['필수 안전 행동 누락']
    else: cached['payload']['decision'] = decision
    atomic_json(path, cached)
    state.save_patch()
    assert recover(state)[0]['status'] == 'ERROR'
    assert state.calls == []


def test_second_failed_review_stops_without_rewrite_or_new_review(state, monkeypatch):
    class Reject:
        def __init__(self, *args): pass
        def review(self, *args, **kwargs):
            state.calls.append(kwargs)
            return review(pass_review=False)
    monkeypatch.setattr('blogbot.recovery.BlogLLM', Reject)
    state.save_patch()
    for _ in range(2):
        assert recover(state)[0]['reason'] == 'source_review_not_approved'
    assert len(state.calls) == 1 and state.media == []


def test_uncertain_review_resumes_cache_only_and_keeps_paid_claim(state, monkeypatch):
    class Interrupted:
        def __init__(self, *args): pass
        def review(self, *args, cache_only, single_attempt):
            state.calls.append(cache_only)
            assert single_attempt
            if not cache_only:
                raise TimeoutError('Provider outcome uncertain')
            raise ResponseFailure('reviewer', 'cached_response_unavailable')
    monkeypatch.setattr('blogbot.recovery.BlogLLM', Interrupted)
    state.save_patch()
    assert recover(state)[0]['status'] == 'ERROR'
    assert recover(state)[0]['reason'] == 'cached_response_unavailable'
    assert state.calls == [False, True] and state.media == []
    changed = {**state.patch, 'body': state.patch['body'] + '\n\n추가 문장'}
    state.save_patch(changed)
    assert recover(state)[0]['status'] == 'ERROR'
    assert state.calls == [False, True]


def test_response_saved_before_packet_interrupt_resumes_without_repurchase(state, monkeypatch):
    import blogbot.recovery as module
    real_atomic = module.atomic_json
    def interrupt(path, value):
        if path.name.endswith('.source-review.json') and 'review' in value:
            raise OSError('Simulated disk interruption')
        real_atomic(path, value)
    monkeypatch.setattr(module, 'atomic_json', interrupt)
    state.save_patch()
    assert recover(state)[0]['status'] == 'ERROR'
    monkeypatch.setattr(module, 'atomic_json', real_atomic)
    assert recover(state)[0]['status'] == 'APPROVED'
    assert [c[2]['cache_only'] for c in state.calls] == [False, True]
    assert len(state.media) == 1


def test_other_ready_post_exhausts_daily_slot_before_review(state):
    state.save_patch()
    other = replace(state.post, request_id='other', category='parenting', status='APPROVED')
    with connect_db(state.settings.db_path) as conn:
        save_post(conn, other)
    assert recover(state)[0]['reason'] == 'source_review_daily_limit'
    assert state.calls == state.media == []


def test_single_review_freezes_enriched_input_and_has_no_truncation_retry(state, monkeypatch):
    import blogbot.llm as module
    client = BlogLLM.__new__(BlogLLM)
    client.client = None
    client.journal = state.settings.db_path.parent / 'usage.jsonl'
    client.review_model, client.reviewer_prompt = 'unchanged-reviewer', 'source reading required'
    enrichment_calls, calls = [], []
    def enrich(directory, request, urls):
        enrichment_calls.append(urls)
        return replace(request, provenance={**request.provenance, 'reference_evidence': [
            {'url': SOURCE, 'text': 'Original article text ' * 40}]})
    def fake(*args, **kwargs):
        calls.append(kwargs)
        return review(), {'id': 'bounded-response', 'output': []}
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', enrich)
    monkeypatch.setattr(module, 'request_json', fake)
    assert client.review(state.post, INFO, state.request, single_attempt=True)['decision'] == 'PASS'
    client.reviewer_prompt = 'Changed later guidance must not change a resumed prompt'
    assert client.review(state.post, INFO, state.request,
                         single_attempt=True, cache_only=True)['decision'] == 'PASS'
    assert len(enrichment_calls) == 1
    assert calls[0]['input'] == calls[1]['input']
    assert calls[0]['cache_context'] == calls[1]['cache_context']
    assert calls[1]['cache_only'] is True
    assert calls[0]['max_output_tokens'] == 6000 and calls[0]['retry_output_tokens'] is None
    assert calls[0]['max_tool_calls'] == 3 and calls[0]['model'] == 'unchanged-reviewer'
    with pytest.raises(ResponseFailure, match='source_review_input_changed'):
        client.review(replace(state.post, body='changed'), INFO, state.request,
                      single_attempt=True, cache_only=True)


def test_single_review_missing_frozen_input_fails_closed(state):
    client = BlogLLM.__new__(BlogLLM)
    client.client = None
    client.journal = state.settings.db_path.parent / 'usage.jsonl'
    client.review_model, client.reviewer_prompt = 'unchanged-reviewer', 'review'
    with pytest.raises(ResponseFailure, match='cached_review_input_unavailable'):
        client.review(state.post, INFO, state.request, single_attempt=True, cache_only=True)


def test_manifest_hash_is_defined_over_normalized_original_and_exact_request(state):
    normalized, issues, _ = inspect_candidate(state.raw, state.request, INFO)
    assert not issues
    assert source_review_hashes(normalized, state.request) == state.patch['review_recovery']
    assert asdict(state.post) == asdict(normalized)
    assert checkpoint_path(state.settings.db_path.parent, state.request.id).exists()


def activate_plan(state):
    plan = {'version': 'fixture-plan', 'date': str(today_kst()), 'target': 1,
            'category': 'exercise', 'editorial_types': ['article'], 'depth': 'deep'}
    state.settings.config['daily_plan'] = plan
    state.request = replace(state.request, provenance={**state.request.provenance, 'daily_plan': plan})
    state.post = replace(state.post, provenance=state.request.provenance)
    path = checkpoint_path(state.settings.db_path.parent, state.request.id)
    checkpoint = json.loads(path.read_text())
    checkpoint['request'] = state.request.prompt_data()
    atomic_json(path, checkpoint)
    with connect_db(state.settings.db_path) as conn, conn:
        conn.execute('UPDATE attempts SET plan_json=?', (json.dumps(plan),))
    state.patch['review_recovery'] = source_review_hashes(state.post, state.request)
    state.save_patch()


@pytest.mark.parametrize('kind', ['receipt_only', 'older_post_saved_today', 'unknown_save_day'])
def test_actual_save_day_and_missing_receipt_gate_before_review(state, kind):
    activate_plan(state)
    with connect_db(state.settings.db_path) as conn, conn:
        if kind == 'receipt_only':
            conn.execute('INSERT INTO save_receipts(request_id,day,category) VALUES(?,?,?)',
                         ('other', str(today_kst()), 'parenting'))
        else:
            other = replace(state.post, request_id='other', category='parenting',
                            as_of_date='2026-09-29', status='SAVED_NAVER')
            post_id = save_post(conn, other)
            if kind == 'older_post_saved_today':
                conn.execute('UPDATE posts SET draft_saved_at=? WHERE id=?',
                             (str(today_kst()) + 'T10:00:00+09:00', post_id))
    result, = recover(state)
    assert result['reason'] == ('save_date_reconciliation_required' if kind == 'unknown_save_day'
                                else 'source_review_daily_limit')
    assert state.calls == state.media == []


@pytest.mark.parametrize('status', ['SAVING', 'SAVE_UNCERTAIN'])
def test_unresolved_save_blocks_review_before_spend(state, status):
    activate_plan(state)
    with connect_db(state.settings.db_path) as conn:
        save_post(conn, replace(state.post, request_id='other', category='parenting',
                                as_of_date='2026-09-29', status=status))
    assert recover(state)[0]['reason'] == 'save_outcome_requires_check'
    assert state.calls == state.media == []


def test_same_daily_recovery_lock_blocks_concurrent_entry(state):
    state.save_patch()
    with media_claim(state.settings.db_path.parent, 'source-review:' + str(today_kst())):
        assert recover(state)[0]['reason'] == 'source_review_in_progress'
    assert state.calls == state.media == []
    assert recover(state)[0]['status'] == 'APPROVED'


def test_finished_post_is_not_downgraded_after_attempt_update_interruption(state):
    state.save_patch()
    assert recover(state)[0]['status'] == 'APPROVED'
    with connect_db(state.settings.db_path) as conn, conn:
        conn.execute("UPDATE attempts SET status='DROP_REVIEW'")
        conn.execute("UPDATE posts SET status='APPROVED'")
    assert recover(state) == []
    assert len(state.calls) == len(state.media) == 1
    with connect_db(state.settings.db_path) as conn:
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == 'APPROVED'


@pytest.fixture
def attribution(state, monkeypatch):
    from blogbot.editorial import routed_category_info
    from blogbot.llm import _review_context
    from blogbot.pre_review import candidate_from_post
    activate_plan(state)
    directory = state.settings.db_path.parent
    previous = replace(state.post, body=state.post.body + '\n\n## 참고자료\n\n- Clinic (기존 출처 설명)')
    previous, issues, _ = inspect_candidate(candidate_from_post(previous), state.request, INFO)
    assert not issues
    checked = review(pass_review=False)
    checked['blocking_issues'] = ['핵심 주장별 원문 열람·근거 대조가 완료되지 않았습니다.']
    checkpoint = checkpoint_path(directory, state.request.id)
    prior_path = checkpoint.with_suffix('.source-review.json')
    prior_input = checkpoint.with_suffix('.source-review-input.json')
    atomic_json(prior_path, {'authorization': source_review_hashes(state.post, state.request),
                            'post': asdict(previous), 'input': asdict(state.request), 'review': checked})
    routed = routed_category_info(INFO, 'exercise', None, None, investment_mode=None)
    identity = hashlib.sha256(json.dumps(
        [state.settings.review_model, BlogLLM._draft_data(previous), routed, state.request.prompt_data()],
        sort_keys=True).encode()).hexdigest()
    atomic_json(prior_input, {'identity': identity, 'prompt': 'Original source review input',
                              'context': _review_context(state.request)})
    replacement = {'old': '(기존 출처 설명)', 'new': '(범위를 명확히 한 출처 설명)'}
    corrected = _attribution_correction(previous, replacement)
    manifest = {'request_id': state.request.id, 'as_of_date': str(today_kst()),
                'replacement': replacement, 'review_attribution': attribution_review_hashes(
                    prior_path.read_bytes(), prior_input.read_bytes(), corrected)}
    path = state.patch_path.with_name(state.patch_path.stem + '-source-review-attribution.enc')
    def save_manifest(value=manifest):
        path.write_bytes(Fernet(os.environ['BLOG_BUNDLE_KEY']).encrypt(
            json.dumps(value, ensure_ascii=False).encode()))
    save_manifest()
    calls = []
    class Reviewer:
        _draft_data = staticmethod(BlogLLM._draft_data)
        def __init__(self, *args):
            assert args[1] == state.settings.openai_model and args[3] == state.settings.review_model
        def review(self, post, info, request, **options):
            calls.append((post, request, options))
            return review()
        def rewrite(self, *args, **kwargs): pytest.fail('No extra writer/rewrite')
    monkeypatch.setattr('blogbot.recovery.BlogLLM', Reviewer)
    protected = {p: p.read_bytes() for p in (
        prior_path, prior_input, state.patch_path, directory / 'usage.jsonl',
        directory / 'auto-recovery.json', revision_path(directory, state.request.id))}
    def recover():
        with connect_db(state.settings.db_path) as conn:
            return recover_attribution_review(state.settings, conn, state.request.id)
    return NS(state=state, previous=previous, corrected=corrected, manifest=manifest,
              save_manifest=save_manifest, path=path, calls=calls, recover=recover,
              protected=protected, prior_path=prior_path, prior_input=prior_input,
              claim=checkpoint.with_suffix('.source-review-attribution.json'))


def test_explicit_attribution_once_preserves_predecessor_and_original_budgets(attribution):
    a = attribution
    assert a.recover()['status'] == 'APPROVED'
    assert len(a.calls) == len(a.state.media) == 1
    post, request, options = a.calls[0]
    assert replace(post, quality_score=0, status=a.corrected.status) == a.corrected
    assert request.id == a.state.request.id
    assert options == {'cache_only': False, 'single_attempt': True, 'attribution_review': True}
    assert a.recover()['status'] == 'APPROVED'
    assert len(a.calls) == 1
    for path, original in a.protected.items():
        assert path.read_bytes() == original
    with connect_db(a.state.settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 1


@pytest.mark.parametrize('field', ['request_sha256', 'previous_claim_sha256', 'previous_input_sha256',
                                  'previous_review_sha256', 'corrected_manuscript_sha256', 'version'])
def test_attribution_manifest_binds_every_identity(attribution, field):
    a = attribution
    manifest = json.loads(json.dumps(a.manifest))
    manifest['review_attribution'][field] = 'wrong'
    a.save_manifest(manifest)
    assert a.recover()['status'] == 'ERROR'
    assert a.calls == [] and not a.claim.exists()


@pytest.mark.parametrize('mutation', ['missing', 'request_id', 'date', 'extra', 'new_source'])
def test_attribution_requires_exact_encrypted_scope(attribution, mutation):
    a = attribution
    manifest = json.loads(json.dumps(a.manifest))
    if mutation == 'missing': a.path.unlink()
    else:
        if mutation == 'request_id': manifest['request_id'] = 'other'
        if mutation == 'date': manifest['as_of_date'] = '2026-09-29'
        if mutation == 'extra': manifest['another_review'] = True
        if mutation == 'new_source': manifest['source_urls'] = ['https://new.example/source']
        a.save_manifest(manifest)
    assert a.recover()['status'] == 'ERROR'
    assert a.calls == []


@pytest.mark.parametrize('part', ['claim', 'input', 'review_missing', 'review_passed', 'unsafe'])
def test_attribution_requires_completed_immutable_rejected_predecessor(attribution, part):
    a = attribution
    if part == 'input':
        a.prior_input.write_bytes(a.prior_input.read_bytes() + b' ')
    else:
        packet = json.loads(a.prior_path.read_text())
        if part == 'claim': packet['post']['body'] += '\nchanged'
        if part == 'review_missing': packet.pop('review')
        if part == 'review_passed': packet['review'] = review()
        if part == 'unsafe': packet['review']['scores'] = [3, 5, 5, 5, 5, 5]
        atomic_json(a.prior_path, packet)
    assert a.recover()['status'] == 'ERROR'
    assert a.calls == []


def test_attribution_failure_never_buys_third_review(attribution, monkeypatch):
    a = attribution
    class Reject:
        _draft_data = staticmethod(BlogLLM._draft_data)
        def __init__(self, *args): pass
        def review(self, *args, **options):
            a.calls.append(options)
            return review(pass_review=False)
    monkeypatch.setattr('blogbot.recovery.BlogLLM', Reject)
    for _ in range(3):
        assert a.recover()['reason'] == 'attribution_review_not_approved'
    assert len(a.calls) == 1 and a.state.media == []
    for path, original in a.protected.items():
        assert path.read_bytes() == original


def test_attribution_uncertainty_is_cache_only_and_changed_patch_cannot_reset_it(attribution, monkeypatch):
    a = attribution
    class Uncertain:
        _draft_data = staticmethod(BlogLLM._draft_data)
        def __init__(self, *args): pass
        def review(self, *args, cache_only, **kwargs):
            a.calls.append(cache_only)
            if not cache_only: raise TimeoutError('uncertain')
            raise ResponseFailure('reviewer', 'cached_response_unavailable')
    monkeypatch.setattr('blogbot.recovery.BlogLLM', Uncertain)
    assert a.recover()['status'] == 'ERROR'
    assert a.recover()['reason'] == 'cached_response_unavailable'
    assert a.calls == [False, True]
    manifest = json.loads(json.dumps(a.manifest))
    manifest['replacement']['new'] = '(다른 출처 설명)'
    corrected = _attribution_correction(a.previous, manifest['replacement'])
    manifest['review_attribution'] = attribution_review_hashes(
        a.prior_path.read_bytes(), a.prior_input.read_bytes(), corrected)
    a.save_manifest(manifest)
    assert a.recover()['status'] == 'ERROR'
    assert a.calls == [False, True]


def test_attribution_response_before_checkpoint_interrupt_uses_exact_cache(attribution, monkeypatch):
    import blogbot.recovery as module
    a = attribution
    atomic = module.atomic_json
    def interrupt(path, payload):
        if path == a.claim and 'review' in payload: raise OSError('disk interruption')
        atomic(path, payload)
    monkeypatch.setattr(module, 'atomic_json', interrupt)
    assert a.recover()['status'] == 'ERROR'
    monkeypatch.setattr(module, 'atomic_json', atomic)
    assert a.recover()['status'] == 'APPROVED'
    assert [call[2]['cache_only'] for call in a.calls] == [False, True]
    assert len(a.state.media) == 1


@pytest.mark.parametrize('status', ['SAVING', 'SAVE_UNCERTAIN'])
def test_attribution_holds_uncertain_save_before_review(attribution, status):
    a = attribution
    with connect_db(a.state.settings.db_path) as conn:
        save_post(conn, replace(a.previous, request_id='other', as_of_date='2026-09-29', status=status))
    assert a.recover()['reason'] == 'save_outcome_requires_check'
    assert a.calls == []


@pytest.mark.parametrize('kind', ['receipt_only', 'older_saved_today', 'unknown_save_day'])
def test_attribution_keeps_actual_save_day_quota(attribution, kind):
    a = attribution
    with connect_db(a.state.settings.db_path) as conn, conn:
        if kind == 'receipt_only':
            conn.execute('INSERT INTO save_receipts(request_id,day,category) VALUES(?,?,?)',
                         ('other', str(today_kst()), 'parenting'))
        else:
            post_id = save_post(conn, replace(a.previous, request_id='other',
                                              as_of_date='2026-09-29', status='SAVED_NAVER'))
            if kind == 'older_saved_today':
                conn.execute('UPDATE posts SET draft_saved_at=? WHERE id=?',
                             (str(today_kst()) + 'T10:00:00+09:00', post_id))
    assert a.recover()['reason'] == ('save_date_reconciliation_required' if kind == 'unknown_save_day'
                                    else 'source_review_daily_limit')
    assert a.calls == []


@pytest.mark.parametrize('change', ['stale_attempt', 'unknown_id', 'wrong_plan', 'pending_attempt'])
def test_attribution_never_adopts_another_or_unfinished_candidate(attribution, change):
    a = attribution
    with connect_db(a.state.settings.db_path) as conn, conn:
        if change == 'stale_attempt': conn.execute("UPDATE attempts SET day='2026-09-29'")
        if change == 'wrong_plan': conn.execute("UPDATE attempts SET plan_json='{}'")
        if change == 'pending_attempt': conn.execute("UPDATE attempts SET status='STARTED'")
        if change == 'unknown_id':
            result = recover_attribution_review(a.state.settings, conn, 'missing-request')
        else:
            result = recover_attribution_review(a.state.settings, conn, a.state.request.id)
    assert result['status'] == 'ERROR' and a.calls == []


def test_attribution_shares_daily_process_lock(attribution):
    a = attribution
    with media_claim(a.state.settings.db_path.parent, 'source-review:' + str(today_kst())):
        assert a.recover()['reason'] == 'source_review_in_progress'
    assert a.calls == []


@pytest.mark.parametrize('old,new,tail', [
    ('', '(수정)', ''), ('(기존 출처 설명)', '다른 설명', ''),
    ('(기존 출처 설명)', '(여러\n문장)', ''), ('(기존 출처 설명)', '(기존 출처 설명)', ''),
    ('(기존 출처 설명)', '(수정)', '\n(기존 출처 설명)'),
    ('(기존 출처 설명)', '(수정)', '\n## 다른 구역\n본문'),
    ('(기존 출처 설명)', '(첫째) 다른 내용 (둘째)', ''),
    ('(기존 출처 설명)', '(안쪽 (둘째))', ''),
])
def test_attribution_replacement_is_one_parenthesis_in_final_reference(attribution, old, new, tail):
    with pytest.raises(ValueError):
        _attribution_correction(replace(attribution.previous, body=attribution.previous.body + tail),
                                {'old': old, 'new': new})


def test_attribution_frozen_prompt_is_separate_and_keeps_call_caps(attribution, monkeypatch):
    from blogbot import llm as module
    a = attribution
    client = BlogLLM.__new__(BlogLLM)
    client.client = None
    client.journal = a.state.settings.db_path.parent / 'usage.jsonl'
    client.review_model, client.reviewer_prompt = a.state.settings.review_model, 'review instructions'
    calls, reads = [], []
    def enrich(directory, request, urls):
        reads.append(urls)
        return replace(request, provenance={**request.provenance, 'reference_evidence': [
            {'url': SOURCE, 'text': 'Direct original source body ' * 20}]})
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', enrich)
    monkeypatch.setattr(module, 'request_json', lambda *args, **kw:
                        (calls.append(kw) or review(), {'output': []}))
    for cache_only in (False, True):
        assert client.review(a.corrected, INFO, a.state.request, cache_only=cache_only,
                             single_attempt=True, attribution_review=True)['decision'] == 'PASS'
    assert len(reads) == 1 and calls[0]['input'] == calls[1]['input']
    assert calls[0]['stage'] == 'reviewer' and calls[0]['request_id'] == a.state.request.id
    assert calls[0]['max_output_tokens'] == 6000 and calls[0]['retry_output_tokens'] is None
    assert calls[0]['max_tool_calls'] == 3 and calls[1]['cache_only'] is True
    assert a.prior_input.read_bytes() == a.protected[a.prior_input]
    assert a.claim.with_name(a.claim.name.replace('.json', '-input.json')).exists()
    with pytest.raises(ValueError):
        client.review(a.corrected, INFO, a.state.request, attribution_review=True)


@pytest.mark.parametrize('scheme', ['https', 'HTTPS'])
def test_attribution_cannot_change_even_already_observed_body_urls(attribution, scheme):
    post = replace(attribution.previous, body=attribution.previous.body.replace(
        '(기존 출처 설명)', f'({scheme}://example.org/original)'))
    with pytest.raises(ValueError, match='body source URLs'):
        _attribution_correction(post, {'old': f'({scheme}://example.org/original)',
                                       'new': f'({scheme}://example.org/other)'})


def test_attribution_prepares_missing_artifact_directory(attribution):
    a = attribution
    a.state.settings.artifact_dir.rmdir()
    assert a.recover()['status'] == 'APPROVED'
    assert a.state.settings.artifact_dir.is_dir()
    assert len(list(a.state.settings.artifact_dir.glob('*.json'))) == 1
