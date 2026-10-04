"""Offline pre-review gates, bounded correction, and resume contracts."""
import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from blogbot.core import connect_db, today_kst
from blogbot.inputs import ContentRequest
from blogbot.llm import BlogLLM, _source_identity, _source_urls
from blogbot.pre_review import (
    DraftCandidate,
    PreReviewFailure,
    checkpoint_path,
    claim_revision,
    inspect_candidate,
    resume_candidate,
    run_pre_review,
)
from blogbot.responses import DRAFT_SCHEMA, ResponseFailure, request_json

URL = 'https://official.example/report?id=1'
FDA = 'https://www.fda.gov/drugs/understanding-over-counter-medicines/sunscreen-how-help-protect-your-skin-sun'
TRACKED_FDA = FDA + '?linkId=100000002918349'
INFO = {'display_name': '투자', 'subcategories': ['시장·산업'], 'rules': []}


def candidate(**overrides):
    body = ('핵심 내용을 먼저 확인해요.\n\n## 사실\n확인된 사실입니다.\n\n'
            '## 의미\n이유를 설명합니다.\n\n## 조건\n조건을 확인해요.\n\n'
            '## 정리\n다음 확인점을 봅니다.\n\n### 변수\n변수를 구분해요.')
    return DraftCandidate({'title': '정책 확인', 'subcategory': '시장·산업',
        'body': body, 'tags': ['정책'], 'source_urls': [URL],
        'as_of_date': str(today_kst()), **overrides}, [URL])


@pytest.fixture
def req():
    return ContentRequest('pre-review-test', 'investment', {'kind': 'policy'})


class NoRepair:
    def correct_draft(self, *args, **kwargs):
        pytest.fail('Good draft or irreparable evidence must not purchase correction')


def test_good_draft_has_free_check_and_no_extra_call(tmp_path, req, capsys):
    post, report = run_pre_review(tmp_path, NoRepair(), candidate(), req, INFO)
    assert post.title == '정책 확인' and report['model_correction_used'] is False
    assert report['changes'] == []
    assert run_pre_review(tmp_path, NoRepair(), candidate(), req, INFO)[0] == post
    events = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert {e['status'] for e in events} == {'PRE_REVIEW_CHECK_STARTED', 'PRE_REVIEW_PASSED'}
    assert '확인된 사실' not in json.dumps(events, ensure_ascii=False)


def test_verified_source_alignment_is_lossless_and_free(tmp_path, req):
    raw = candidate(source_urls=[FDA], body=candidate().payload['body'] + '\n' + FDA)
    raw.observed = [TRACKED_FDA]
    post, report = run_pre_review(tmp_path, NoRepair(), raw, req, INFO)
    assert post.source_urls == [TRACKED_FDA]
    assert post.body == raw.payload['body'].replace(FDA, TRACKED_FDA)
    assert report['changes'] == ['observed_source_alignment']
    assert not report['model_correction_used']


@pytest.mark.parametrize('other', [
    FDA + '?linkId=100000002918350', FDA + '?linkId=100000002918349&document=2',
    FDA.replace('/sunscreen-', '/other-') + '?linkId=100000002918349',
    TRACKED_FDA.replace('www.fda.gov', 'evil.example'), TRACKED_FDA.replace('https:', 'http:'),
    'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261004000002',
])
def test_narrow_alias_never_erases_other_document_identities(other):
    target = ('https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261004000001'
              if 'dart.fss' in other else FDA)
    assert _source_identity(other) != _source_identity(target)
    with pytest.raises(ValueError):
        _source_urls({'source_urls': [target]}, [other])


@pytest.mark.parametrize('change,observed,code', [
    ({'source_urls': ['https://invented.example/report']}, [URL], 'unobserved_source_url'),
    ({'source_urls': [URL.replace('id=1', 'id=2')]}, [URL], 'unobserved_source_url'),
    ({'body': candidate().payload['body'] + '\nhttps://invented.example/body'}, [URL], 'unobserved_body_url'),
    ({}, [], 'no_observed_evidence'),
    ({'tags': 42, 'source_urls': [URL.replace('id=1', 'id=2')]}, [URL], 'unobserved_source_url'),
    ({'tags': 42, 'body': 'https://invented.example/body'}, [URL], 'unobserved_body_url'),
])
def test_irreparable_evidence_never_buys_correction(tmp_path, req, change, observed, code):
    raw = candidate(**change)
    raw.observed = observed
    with pytest.raises(PreReviewFailure) as exc:
        run_pre_review(tmp_path, NoRepair(), raw, req, INFO)
    assert code in exc.value.codes
    assert exc.value.stage == 'pre_review_check'


@pytest.mark.parametrize('field,value', [('tags', 42), ('title', None), ('body', ['wrong']),
                                        ('as_of_date', 'yesterday'), ('subcategory', 'invalid')])
def test_invalid_fields_are_corrected_once_then_revalidated(tmp_path, req, field, value):
    calls = []
    class Repair:
        def correct_draft(self, raw, info, codes, req, *, cache_only):
            calls.append((codes, cache_only))
            return candidate()
    post, report = run_pre_review(tmp_path, Repair(), candidate(**{field: value}), req, INFO)
    assert post.title == '정책 확인' and len(calls) == 1 and calls[0][1] is False
    assert report['model_correction_used']
    assert run_pre_review(tmp_path, NoRepair(), candidate(**{field: value}), req, INFO)[0] == post


def test_missing_fields_are_not_coerced_or_silently_defaulted(tmp_path, req):
    raw = candidate()
    del raw.payload['as_of_date']
    class Repair:
        def correct_draft(self, raw, info, codes, req, **kw):
            assert codes == ['missing_field_as_of_date']
            return candidate()
    assert run_pre_review(tmp_path, Repair(), raw, req, INFO)[1]['model_correction_used']


@pytest.mark.parametrize('body,code', [
    ('작성일: 2026-10-04\n\n' + candidate().payload['body'], 'invalid_preview'),
    ('본문만 있습니다.', 'missing_headings'),
    (candidate().payload['body'] + '\n![image](file.png)', 'inline_image_markup'),
    (candidate().payload['body'] + '\n저는 이 방법을 사용했더니 효과를 느꼈어요.', 'unsupported_personal_experience'),
    (candidate().payload['body'] + '\n선우는 9개월입니다.', 'unsupported_child_age'),
])
def test_structure_format_and_supplied_experience_boundaries(req, body, code):
    _, codes, _ = inspect_candidate(candidate(body=body), req, INFO)
    assert code in codes


def test_owner_provided_experience_and_age_are_allowed(req):
    sentence = '저는 이 방법을 사용했더니 효과를 느꼈어요'
    req = replace(req, data={**req.data, 'context': sentence, 'age_months': 9})
    _, codes, _ = inspect_candidate(candidate(body=candidate().payload['body'] + '\n' + sentence
                                             + '.\n선우는 9개월입니다.'), req, INFO)
    assert codes == []


def test_correction_cannot_add_unobserved_source_and_never_retries(tmp_path, req):
    calls = []
    class Repair:
        def correct_draft(self, *args, **kw):
            calls.append(kw)
            return candidate(source_urls=['https://invented.example/'])
    raw = candidate(tags=42)
    for _ in range(2):
        with pytest.raises(PreReviewFailure, match='unobserved_source_url') as exc:
            run_pre_review(tmp_path, Repair(), raw, req, INFO)
        assert exc.value.stage == 'pre_review_recheck'
    assert len(calls) == 1


def test_uncertain_correction_can_only_resume_cached_response(tmp_path, req):
    calls = []
    class Repair:
        def correct_draft(self, *a, cache_only):
            calls.append(cache_only)
            if cache_only:
                raise ResponseFailure('pre_review_correction', 'cached_response_unavailable')
            raise TimeoutError('private provider details')
    raw = candidate(tags=42)
    with pytest.raises(PreReviewFailure, match='correction_failed'):
        run_pre_review(tmp_path, Repair(), raw, req, INFO)
    with pytest.raises(ResponseFailure, match='cached_response_unavailable'):
        run_pre_review(tmp_path, Repair(), resume_candidate(tmp_path, req), req, INFO)
    assert calls == [False, True]


def test_completed_correction_resumes_after_checkpoint_interrupt(tmp_path, req, monkeypatch):
    import blogbot.pre_review as pre
    calls = []
    real_atomic = pre.atomic_json
    def interrupt(path, packet):
        if 'corrected' in packet:
            raise OSError('simulated disk interruption')
        real_atomic(path, packet)
    class Repair:
        def correct_draft(self, *a, cache_only):
            calls.append(cache_only)
            return candidate()
    raw = candidate(tags=42)
    monkeypatch.setattr(pre, 'atomic_json', interrupt)
    with pytest.raises(OSError):
        run_pre_review(tmp_path, Repair(), raw, req, INFO)
    monkeypatch.setattr(pre, 'atomic_json', real_atomic)
    post, report = run_pre_review(tmp_path, Repair(), raw, req, INFO)
    assert post and report['model_correction_used'] and calls == [False, True]


def test_changed_input_cannot_reset_paid_slot(tmp_path, req):
    class Repair:
        def correct_draft(self, *a, **kw): return candidate()
    run_pre_review(tmp_path, Repair(), candidate(tags=42), req, INFO)
    with pytest.raises(PreReviewFailure, match='pre_review_input_changed'):
        run_pre_review(tmp_path, NoRepair(), candidate(title='changed'), req, INFO)
    with pytest.raises(PreReviewFailure, match='manuscript_correction_limit'):
        claim_revision(tmp_path, req.id, 'rewrite', {})


def test_exclusive_slot_is_cache_only_for_identical_claim(tmp_path, req):
    checkpoint_path(tmp_path, req.id).parent.mkdir()
    assert claim_revision(tmp_path, req.id, 'pre_review_correction', {'a': 1}) is False
    assert claim_revision(tmp_path, req.id, 'pre_review_correction', {'a': 1}) is True
    with pytest.raises(PreReviewFailure):
        claim_revision(tmp_path, req.id, 'pre_review_correction', {'a': 2})


def test_completed_raw_missing_fields_cached_for_pre_review(tmp_path):
    calls = []
    response = NS(status='completed', output_text='{}', output=[], id='raw')
    response.model_dump = lambda: {'status': 'completed', 'output': [], 'id': 'raw'}
    client = NS(responses=NS(create=lambda **kw: calls.append(kw) or response))
    kwargs = {'model': 'offline', 'stage': 'writer', 'request_id': 'raw', 'schema': DRAFT_SCHEMA,
              'journal': tmp_path/'usage.jsonl', 'max_output_tokens': 12000, 'input': 'fixture',
              'validate_required': False}
    assert request_json(client, **kwargs)[0] == {}
    assert request_json(client, cache_only=True, **kwargs)[0] == {}
    assert len(calls) == 1


def test_correction_call_has_no_search_no_token_retry_and_reuses_exact_cache(tmp_path, req, monkeypatch):
    import blogbot.llm as mod
    client = BlogLLM.__new__(BlogLLM)
    client.client, client.journal = None, tmp_path/'usage.jsonl'
    client.model, client.writer_prompt = 'offline', 'writer-contract'
    captured = []
    def fake(*a, **kw):
        captured.append(kw)
        return candidate().payload, {'output': []}
    monkeypatch.setattr(mod, 'request_json', fake)
    client.correct_draft(candidate(tags=42), INFO, ['invalid_field_tags'], req)
    client.correct_draft(candidate(tags=42), INFO, ['invalid_field_tags'], req, cache_only=True)
    assert captured[0]['input'] == captured[1]['input']
    assert captured[0]['stage'] == 'pre_review_correction'
    assert captured[1]['cache_only'] is True
    assert 'tools' not in captured[0] and 'retry_output_tokens' not in captured[0]
    assert captured[0]['max_output_tokens'] == 12000


def test_bounded_editorial_rewrite_replays_enriched_prompt_exactly(tmp_path, req, monkeypatch):
    import blogbot.llm as mod
    client = BlogLLM.__new__(BlogLLM)
    client.client, client.journal = None, tmp_path/'usage.jsonl'
    client.model, client.writer_prompt = 'offline', 'writer'
    post, _, _ = inspect_candidate(candidate(), req, INFO)
    calls = []
    def fake(*a, **kw):
        calls.append(kw)
        return candidate().payload, {'output': []}
    monkeypatch.setattr(mod, 'request_json', fake)
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u:
                        replace(r, provenance={**r.provenance, 'reference_evidence': []}))
    client.rewrite(post, INFO, {}, req, single_attempt=True)
    client.rewrite(post, INFO, {}, req, single_attempt=True, cache_only=True)
    assert calls[0]['input'] == calls[1]['input']
    assert calls[0]['retry_output_tokens'] is None


def test_new_prechecked_rejection_cannot_enter_legacy_paid_recovery(tmp_path, req, monkeypatch):
    from blogbot.recovery import recover_rejected
    run_pre_review(tmp_path, NoRepair(), candidate(), req, INFO)
    settings = NS(db_path=tmp_path/'blog.db', config={'categories': {'investment': {'max_daily': 1}}})
    with connect_db(settings.db_path) as conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), req.category, req.id, 'DROP_REVIEW'))
        monkeypatch.setattr('blogbot.recovery.BlogLLM', lambda *a: pytest.fail('No legacy purchase'))
        result = recover_rejected(settings, conn, [req])
    assert result[0]['reason'] == 'review_rejected_after_pre_review'


@pytest.mark.parametrize('changes,category,code', [
    ({'source_urls': [], 'body': 'https://invented.example/'}, 'investment', 'unobserved_body_url'),
    ({'source_urls': 42, 'body': 'https://invented.example/'}, 'investment', 'unobserved_body_url'),
    ({'as_of_date': 'invalid'}, 'cooking', 'missing_owner_photos'),
    ({'subcategory': 'invalid', 'body': '수익 보장 문구'}, 'investment', 'guaranteed_return_language'),
])
def test_mixed_errors_never_mask_hard_stops(tmp_path, req, changes, category, code):
    req = replace(req, category=category)
    with pytest.raises(PreReviewFailure) as exc:
        run_pre_review(tmp_path, NoRepair(), candidate(**changes), req, INFO)
    assert code in exc.value.codes


@pytest.fixture
def pipeline_settings(tmp_path, monkeypatch, weekday_clock):
    from blogbot.config import load_settings
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    settings = load_settings()
    settings.config['community']['enabled'] = False
    settings.config['topics'].pop('scheduled', None)
    return settings


def pipeline_setup(monkeypatch, req, fake):
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: ([req], []))
    monkeypatch.setattr('blogbot.pipeline.prepare_request', lambda s, r: r)
    monkeypatch.setattr('blogbot.pipeline.rank_candidates', lambda *a: [req])
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', fake)


def review(decision='PASS'):
    return {'scores': [5]*6, 'total': 30, 'decision': decision, 'issues': [],
            'blocking_issues': [], 'rewrite_instructions': ''}


def test_pipeline_correction_precedes_strict_review_and_shares_revision_budget(
        pipeline_settings, req, monkeypatch, capsys):
    from blogbot.pipeline import run_daily
    calls = []
    class LLM:
        def __init__(self, *a): pass
        def create_draft(self, *a, **kw): calls.append('writer'); return candidate(tags=42)
        def correct_draft(self, *a, **kw): calls.append('correction'); return candidate()
        def review(self, *a): calls.append('reviewer'); return review('REWRITE')
        def rewrite(self, *a, **kw): pytest.fail('Correction already consumed shared revision slot')
    pipeline_setup(monkeypatch, req, LLM)
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda *a: pytest.fail('Unapproved'))
    results = run_daily(pipeline_settings, count=1)
    assert results[0]['status'] == 'DROP_REVIEW'
    assert results[0]['pre_review']['model_correction_used']
    assert calls == ['writer', 'correction', 'reviewer']
    run_daily(pipeline_settings, count=1, retry_failed=True)
    assert calls == ['writer', 'correction', 'reviewer']
    logs = capsys.readouterr().out
    assert logs.index('PRE_REVIEW_CORRECTION_STARTED') < logs.index('PRE_REVIEW_RECHECK_STARTED')
    assert 'invalid_field_tags' in logs and '정책 확인' not in logs


def test_pipeline_good_draft_keeps_formal_reviewer_then_media_order(pipeline_settings, req, monkeypatch):
    from blogbot.pipeline import run_daily
    calls = []
    class LLM:
        def __init__(self, *a): pass
        def create_draft(self, *a, **kw): calls.append('writer'); return candidate()
        def correct_draft(self, *a, **kw): pytest.fail('No extra call for correct drafts')
        def review(self, *a): calls.append('reviewer'); return review()
    pipeline_setup(monkeypatch, req, LLM)
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda s, r, p: calls.append('images') or p)
    assert run_daily(pipeline_settings, count=1)[0]['status'] == 'APPROVED'
    assert calls == ['writer', 'reviewer', 'images']


def test_pipeline_formal_rewrite_keeps_observed_body_links(pipeline_settings, req, monkeypatch):
    from blogbot.pipeline import run_daily
    calls = []
    extra = 'https://official.example/appendix'
    class LLM:
        def __init__(self, *a): self.reviews = 0
        def create_draft(self, *a, **kw): return candidate()
        def review(self, *a):
            self.reviews += 1
            return review('REWRITE' if self.reviews == 1 else 'PASS')
        def rewrite(self, *a, **kw):
            calls.append(kw)
            raw = candidate(body=candidate().payload['body'] + '\n' + extra)
            raw.observed.append(extra)
            return raw
    pipeline_setup(monkeypatch, req, LLM)
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda s, r, p: p)
    assert run_daily(pipeline_settings, count=1)[0]['status'] == 'APPROVED'
    assert calls == [{'cache_only': False, 'single_attempt': True, 'raw': True}]


def test_pipeline_repair_failure_has_safe_phase_and_never_reviews(pipeline_settings, req, monkeypatch):
    from blogbot.pipeline import run_daily
    calls = []
    class LLM:
        def __init__(self, *a): pass
        def create_draft(self, *a, **kw): calls.append('writer'); return candidate(tags=42)
        def correct_draft(self, *a, **kw): calls.append('correction'); return candidate(tags=42)
        def review(self, *a): pytest.fail('Bad repair must not reach reviewer')
    pipeline_setup(monkeypatch, req, LLM)
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda *a: pytest.fail('Unapproved'))
    result = run_daily(pipeline_settings, count=1)[0]
    assert result['status'] == 'ERROR' and result['stage'] == 'pre_review_recheck'
    assert result['reason'] == 'invalid_field_tags'
    assert run_daily(pipeline_settings, count=1, retry_failed=True)[0]['status'] == 'ERROR'
    assert calls == ['writer', 'correction']


def test_encrypted_transport_keeps_correction_claim_and_reuses_completed_packet(
        pipeline_settings, req, tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from blogbot.cloud import extract_bundle, pack
    from blogbot.pre_review import revision_path
    directory = pipeline_settings.db_path.parent
    class Repair:
        def correct_draft(self, *a, **kw): return candidate()
    raw = candidate(tags=42)
    original, _ = run_pre_review(directory, Repair(), raw, req, INFO)
    claim = revision_path(directory, req.id).read_bytes()
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    bundle = tmp_path/'transport.enc'
    pack(pipeline_settings, bundle)
    target = tmp_path/'relocated'
    extract_bundle(bundle.read_bytes(), target, key)
    assert revision_path(target, req.id).read_bytes() == claim
    restored, report = run_pre_review(target, NoRepair(), resume_candidate(target, req), req, INFO)
    assert restored == original and report['model_correction_used']


def test_invalid_list_member_does_not_hide_unknown_string_source(tmp_path, req):
    with pytest.raises(PreReviewFailure) as exc:
        run_pre_review(tmp_path, NoRepair(), candidate(source_urls=['https://invented.example/', 42]),
                       req, INFO)
    assert 'unobserved_source_url' in exc.value.codes


def test_provided_experience_reflow_does_not_trigger_paid_repair(tmp_path, req):
    req = replace(req, data={**req.data, 'context': '저는 이 방법을\n사용했더니 효과를 느꼈어요'})
    raw = candidate(body=candidate().payload['body'] + '\n저는 이 방법을 사용했더니 효과를 느꼈어요.')
    assert run_pre_review(tmp_path, NoRepair(), raw, req, INFO)[1]['model_correction_used'] is False


def test_legacy_approval_repair_cannot_bypass_new_correction_budget(
        pipeline_settings, req, monkeypatch):
    from dataclasses import asdict

    from blogbot.core import save_post
    from blogbot.images import atomic_json
    from blogbot.pipeline import run_daily
    dart = 'https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261007000001'
    raw = candidate(source_urls=[dart])
    raw.observed = [dart]
    post, _ = run_pre_review(pipeline_settings.db_path.parent, NoRepair(), raw, req, INFO)
    post.status, post.quality_score = 'APPROVED', 30
    with connect_db(pipeline_settings.db_path) as conn:
        post_id = save_post(conn, post)
    pipeline_settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    atomic_json(pipeline_settings.artifact_dir/f'{today_kst()}-{post_id:05d}.json',
                {'post': asdict(post), 'input': asdict(req), 'review': review()})
    pipeline_setup(monkeypatch, req, lambda *a: pytest.fail('No legacy LLM bypass'))
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: ([], []))
    monkeypatch.setattr('blogbot.pipeline.rank_candidates', lambda *a: [])
    result = run_daily(pipeline_settings, count=1, retry_failed=True)
    assert result[0]['reason'] == 'pre_review_recovery_guard'
    with connect_db(pipeline_settings.db_path) as conn:
        assert conn.execute('SELECT status FROM posts WHERE id=?', (post_id,)).fetchone()[0] == 'REPAIR_PENDING'


def test_parallel_correction_claims_never_buy_twice(tmp_path, req):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    calls, started, release = [], Event(), Event()
    class Repair:
        def correct_draft(self, *a, cache_only):
            calls.append(cache_only)
            if cache_only:
                raise ResponseFailure('pre_review_correction', 'cached_response_unavailable')
            started.set()
            assert release.wait(5)
            return candidate()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_pre_review, tmp_path, Repair(), candidate(tags=42), req, INFO)
        assert started.wait(5)
        try:
            with pytest.raises(ResponseFailure, match='cached_response_unavailable'):
                run_pre_review(tmp_path, Repair(), candidate(tags=42), req, INFO)
        finally:
            release.set()
        assert first.result()[1]['model_correction_used']
    assert calls == [False, True]


def test_real_correction_adapter_uses_response_cache_and_never_retries_truncation(tmp_path, req):
    client = BlogLLM.__new__(BlogLLM)
    client.journal, client.model, client.writer_prompt = tmp_path/'usage.jsonl', 'offline', 'writer'
    calls = []
    completed = NS(status='completed', output_text=json.dumps(candidate().payload),
                   output=[], id='correction-response')
    completed.model_dump = lambda: {'status': 'completed', 'output': [], 'id': 'correction-response'}
    client.client = NS(responses=NS(create=lambda **kw: calls.append(kw) or completed))
    raw = candidate(tags=42)
    assert client.correct_draft(raw, INFO, ['invalid_field_tags'], req).payload == candidate().payload
    assert client.correct_draft(raw, INFO, ['invalid_field_tags'], req, cache_only=True).payload == candidate().payload
    assert len(calls) == 1 and calls[0]['max_output_tokens'] == 12000
    incomplete = NS(status='incomplete', output_text='', output=[], id='truncated',
                    incomplete_details={'reason': 'max_output_tokens'})
    client.client = NS(responses=NS(create=lambda **kw: calls.append(kw) or incomplete))
    with pytest.raises(ResponseFailure, match='max_output_tokens'):
        client.correct_draft(raw, INFO, ['invalid_field_tags'], replace(req, id='different'))
    assert len(calls) == 2


def test_source_article_first_person_is_not_owner_experience(req):
    sentence = '저는 이 방법을 사용했더니 효과를 느꼈어요'
    req = replace(req, data={**req.data, 'body': sentence})
    _, codes, _ = inspect_candidate(candidate(body=candidate().payload['body'] + '\n' + sentence), req, INFO)
    assert 'unsupported_personal_experience' in codes
