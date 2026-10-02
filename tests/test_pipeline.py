import json
from contextlib import closing

import pytest

from blogbot.config import load_settings
from blogbot.core import (
    PostDraft,
    connect_db,
    reserve_attempt,
    review_result,
    save_post,
    today_kst,
)
from blogbot.inputs import ContentRequest
from blogbot.llm import _source_urls
from blogbot.pipeline import run_daily, save_pending


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)
    monkeypatch.setattr('blogbot.recovery.prepare_reference_evidence', lambda d, r, u: r)
    monkeypatch.setenv("BLOG_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NAVER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("NAVER_BLOG_ID", "example")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder")
    config = load_settings()
    config.config['topics'].pop('scheduled', None)
    config.config["community"]["enabled"] = False
    return config


def draft():
    return PostDraft(
        "cooking", "집밥", "국 끓이는 순서", "국 끓이는 순서와 재료 준비",
        "재료를 계량하고 충분히 가열합니다.", ["집밥"],
        ["https://www.foodsafetykorea.go.kr/"], today_kst().isoformat(),
        27, "APPROVED",
    )


def test_quota_survives_connection_restart(settings):
    # Isolate the total daily cap from the production one-post category cap.
    settings.config["categories"]["parenting"]["max_daily"] = 3
    candidates = [ContentRequest(f"q-{i}", "parenting", {"question": "월령별 발달 과정"})
                  for i in range(3)]
    for _ in range(2):
        with closing(connect_db(settings.db_path)) as conn:
            assert reserve_attempt(conn, settings.config, 2, candidates)
    with closing(connect_db(settings.db_path)) as conn:
        assert reserve_attempt(conn, settings.config, 2, candidates) is None


def test_review_rejects_inconsistent_or_unsafe_scores():
    assert review_result({"scores": [5] * 6, "total": 29, "decision": "PASS"})[1] == "DROP"
    assert review_result({
        "scores": [2, 5, 5, 5, 5, 5], "total": 27, "decision": "PASS",
    })[1] != "PASS"


def test_high_score_cannot_ignore_actionable_review_corrections():
    assert review_result({'scores': [5, 5, 5, 4, 5, 4], 'total': 28, 'decision': 'PASS',
                          'issues': ['수면 안전 문장을 수정'],
                          'rewrite_instructions': '영아 잠자리 인형 권고를 삭제'})[1] == 'REWRITE'


@pytest.mark.parametrize('status', ['ERROR', 'RESEARCH_REQUIRED'])
def test_recovery_retries_failed_category_and_fills_new_category(settings, monkeypatch, status):
    requests = [ContentRequest('failed-exercise', 'exercise', {'question': '운동 자세'}),
                ContentRequest('new-investment', 'investment', {'kind': 'research'})]
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), 'exercise', 'failed-exercise', status))
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), 'parenting', 'already-ready', 'APPROVED'))
    calls = []
    class FakeLLM:
        def __init__(self, *args):
            pass
        def create_draft(self, request, info, titles):
            calls.append(request.id)
            post = draft()
            post.category, post.subcategory = request.category, info['subcategories'][0]
            post.request_id, post.title = request.id, request.id
            return post
        def review(self, *args):
            return {'scores': [5]*6, 'total': 30, 'decision': 'PASS'}
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: (requests, []))
    monkeypatch.setattr('blogbot.pipeline.rank_candidates', lambda *args: requests)
    monkeypatch.setattr('blogbot.pipeline.prepare_request', lambda settings, request: request)
    monkeypatch.setattr('blogbot.pipeline.validate_structure', lambda *args: None)
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda settings, request, post: post)
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', FakeLLM)
    results = run_daily(settings, retry_failed=True)
    assert calls == ['failed-exercise', 'new-investment']
    assert all(r['status'] == 'APPROVED' for r in results)
    with closing(connect_db(settings.db_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts WHERE day=?',
                            (str(today_kst()),)).fetchone()[0] == 3


def test_writer_cannot_invent_source_url():
    with pytest.raises(ValueError):
        _source_urls({"source_urls": ["https://invented.example/"]}, ["https://actual.example/"])


def test_unknown_save_is_not_automatically_repeated(settings, monkeypatch):
    with closing(connect_db(settings.db_path)) as conn:
        save_post(conn, draft())

    class FailingWriter:
        def preflight(self, post):
            pass

        def save(self, post):
            raise RuntimeError("No save confirmation")

    monkeypatch.setattr("blogbot.pipeline.make_writer", lambda _: FailingWriter())
    assert save_pending(settings)[0]["status"] == "SAVE_UNCERTAIN"
    assert save_pending(settings)[0]["status"] == "MANUAL_CHECK_REQUIRED"


def test_rewrite_duplicate_is_blocked(settings, monkeypatch):
    first = draft()
    with closing(connect_db(settings.db_path)) as conn:
        save_post(conn, first)

    class FakeLLM:
        def __init__(self, *args):
            self.reviews = 0

        def create_draft(self, request, info, recent):
            post = draft()
            post.category, post.subcategory = request.category, info["subcategories"][0]
            post.title = "완전히 다른 주제의 작성 후보"
            return post

        def review(self, post, info, request):
            self.reviews += 1
            scores = [4, 3, 3, 3, 3, 4] if self.reviews == 1 else [5] * 6
            return {"scores": scores, "total": sum(scores),
                    "decision": "REWRITE" if self.reviews == 1 else "PASS"}

        def rewrite(self, post, info, review, request):
            post.title = first.title
            return post

    monkeypatch.setattr("blogbot.pipeline.BlogLLM", FakeLLM)
    candidate = ContentRequest("question-1", "parenting", {"question": "월령별 발달 과정"})
    monkeypatch.setattr("blogbot.pipeline.collect_requests", lambda _: ([candidate], []))
    monkeypatch.setattr("blogbot.pipeline.prepare_request", lambda settings, request: request)
    monkeypatch.setattr("blogbot.pipeline.validate_structure", lambda post, required: None)
    assert run_daily(settings, count=1)[0]["status"] == "DROP_DUPLICATE"


def test_only_supplied_category_is_reserved_once(settings):
    request = ContentRequest("question-1", "parenting", {"question": "월령별 발달 과정"})
    with closing(connect_db(settings.db_path)) as conn:
        assert reserve_attempt(conn, settings.config, 3, [request])[1].category == "parenting"
        assert reserve_attempt(conn, settings.config, 3, [request]) is None


def test_one_review_rejection_can_be_replaced_with_bounded_attempt(settings):
    requests = [ContentRequest(f"investment-{i}", "investment", {"kind": "research"})
                for i in range(3)]
    with closing(connect_db(settings.db_path)) as conn:
        first, _ = reserve_attempt(conn, settings.config, 3, requests)
        with conn:
            conn.execute("UPDATE attempts SET status='DROP_REVIEW' WHERE id=?", (first,))
            for category in ("parenting", "exercise"):
                conn.execute("INSERT INTO attempts(day, category, request_id, status) "
                             "VALUES (?, ?, ?, 'APPROVED')",
                             (today_kst().isoformat(), category, category))
        _second, request = reserve_attempt(conn, settings.config, 3, requests)
        assert request.id == "investment-1"
        assert reserve_attempt(conn, settings.config, 3, requests) is None


def test_empty_input_makes_no_paid_calls(settings, monkeypatch):
    def fail_if_called(*args):
        pytest.fail("Empty inputs must not call the LLM")

    monkeypatch.setattr("blogbot.pipeline.BlogLLM", fail_if_called)
    assert run_daily(settings, count=3) == [{"status": "NO_ELIGIBLE_INPUT_OR_DAILY_LIMIT"}]


@pytest.mark.parametrize('kind', ['research', 'policy'])
def test_reports_and_policy_precede_disclosures_even_after_trend_sort(settings, monkeypatch, kind):
    candidates = [ContentRequest('disclosure', 'investment', {'kind': 'disclosure'}),
                  ContentRequest('preferred', 'investment', {'kind': kind})]
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: (candidates, []))
    monkeypatch.setattr('blogbot.pipeline.rank_candidates', lambda *a: candidates)

    def stop_before_paid_call(settings, request):
        raise ValueError('stop before generation')

    monkeypatch.setattr('blogbot.pipeline.prepare_request', stop_before_paid_call)
    assert run_daily(settings, count=1)[0]['request_id'] == 'preferred'


def test_recovery_reuses_failed_reservation_before_new_missing_category(settings, monkeypatch):
    candidates = [ContentRequest('failed', 'parenting', {'question': '질문'}),
                  ContentRequest('unused', 'exercise', {'question': '운동'})]
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), 'parenting', 'failed', 'ERROR'))
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: (candidates, []))

    def stop_before_paid_call(settings, request):
        raise ValueError('stop before generation')

    monkeypatch.setattr('blogbot.pipeline.prepare_request', stop_before_paid_call)
    result = run_daily(settings, count=3, retry_failed=True)
    assert [r['request_id'] for r in result] == ['failed', 'unused']
    with closing(connect_db(settings.db_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 2


def test_legacy_approval_repair_keeps_identity_and_is_not_repeated(settings, monkeypatch):
    import json
    from dataclasses import asdict

    from blogbot.images import atomic_json
    post = draft()
    post.category, post.subcategory = 'parenting', settings.config['categories']['parenting']['subcategories'][0]
    post.request_id = 'legacy-approved'
    request = ContentRequest(post.request_id, 'parenting', {'question': '실제 질문'})
    with closing(connect_db(settings.db_path)) as conn, conn:
        post_id = save_post(conn, post)
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), 'parenting', post.request_id, 'APPROVED'))
    settings.artifact_dir.mkdir(parents=True)
    path = settings.artifact_dir/f'{post.as_of_date}-{post_id:05d}.json'
    atomic_json(path, {'post': asdict(post), 'input': asdict(request), 'review': {
        'scores': [5]*6, 'total': 30, 'decision': 'PASS', 'issues': ['안전 문장 수정'],
        'rewrite_instructions': '위험한 권고 삭제'}})
    calls = []
    class FakeLLM:
        def __init__(self, *args):
            pass
        def rewrite(self, post, *args, **kwargs):
            calls.append(post.request_id)
            assert post.status == 'APPROVED'
            if len(calls) == 1:
                raise ValueError('deterministic validation failure after cached response')
            assert kwargs == {'cache_only': True}
            post.body += '\n보완된 안전 문장'
            return post
        def review(self, *args):
            return {'scores': [5]*6, 'total': 30, 'decision': 'PASS', 'issues': [],
                    'rewrite_instructions': ''}
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: ([], []))
    monkeypatch.setattr('blogbot.pipeline.rank_candidates', lambda *args: [])
    monkeypatch.setattr('blogbot.pipeline.validate_structure', lambda *args: None)
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda settings, request, post: post)
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', FakeLLM)
    assert run_daily(settings, retry_failed=True)[0]['status'] == 'ERROR'
    assert run_daily(settings, retry_failed=True)[0]['status'] == 'APPROVED'
    run_daily(settings, retry_failed=True)
    assert calls == [post.request_id, post.request_id]
    assert json.loads(path.read_text())['post']['request_id'] == post.request_id
    with closing(connect_db(settings.db_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1


def test_source_normalization_keeps_document_and_stock_identity():
    old = 'https://finance.naver.com/item/main.naver?code=123456&utm_source=test'
    new = 'https://stock.naver.com/domestic/stock/123456/price'
    assert _source_urls({'source_urls': [old]}, [new]) == [old]
    with pytest.raises(ValueError):
        _source_urls({'source_urls': [old]}, [new.replace('123456', '654321')])
    with pytest.raises(ValueError):
        _source_urls({'source_urls': ['https://example.com/report?id=2']},
                     ['https://example.com/report?id=1'])


def test_encoded_tracking_suffix_preserves_the_actual_document():
    url = 'https://www.acefitness.org/resources/everyone/exercise-library/158/seated-lat-pulldown/'
    assert _source_urls({'source_urls': [url]}, [url + '%3Fsrsltid%3Dtracking']) == [url]
    with pytest.raises(ValueError):
        _source_urls({'source_urls': [url]}, [url.replace('/158/', '/159/') + '%3Fsrsltid%3Dtracking'])


def test_rewrite_preserves_original_reference_date(monkeypatch):
    from blogbot.llm import BlogLLM
    post = draft()
    llm = object.__new__(BlogLLM)
    llm.writer_prompt, llm.model, llm.client, llm.journal = '', 'test', None, None
    monkeypatch.setattr('blogbot.llm.request_json', lambda *args, **kwargs: (
        {'as_of_date': '2026-09-29', 'source_urls': post.source_urls}, {'output': []}))
    rewritten = llm.rewrite(post, {}, {}, ContentRequest('date-test', post.category, {}))
    assert rewritten.as_of_date == post.as_of_date


def test_rejected_recovery_reuses_identity_and_caps_paid_revision(settings, monkeypatch):
    from dataclasses import asdict

    from blogbot.recovery import recover_rejected
    post = draft()
    post.category, post.subcategory = 'parenting', '육아생활'
    post.request_id, post.status = 'rejected-existing', 'DROP_REVIEW'
    request = ContentRequest(post.request_id, post.category, {'question': '원본 질문'})
    settings.artifact_dir.mkdir(parents=True)
    (settings.db_path.parent/'response-cache').mkdir()
    monkeypatch.setattr('blogbot.recovery.prepare_request', lambda _, r: r)
    monkeypatch.setattr('blogbot.recovery.rejected_checkpoint', lambda *a: {
        'post': asdict(post), 'input': asdict(request), 'review': {'decision': 'REWRITE'}})
    monkeypatch.setattr('blogbot.recovery.validate_structure', lambda *a: None)
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda s, r, p: p)
    calls = []
    class FakeLLM:
        def __init__(self, *a): pass
        def rewrite(self, post, *a, cache_only=False):
            calls.append(cache_only)
            if not cache_only: raise ValueError('failure after completed cached response')
            post.body += ' 검증된 수정'
            return post
        def review(self, *a):
            return {'scores': [5]*6, 'total': 30, 'decision': 'PASS', 'issues': [],
                    'rewrite_instructions': ''}
    monkeypatch.setattr('blogbot.recovery.BlogLLM', FakeLLM)
    with closing(connect_db(settings.db_path)) as conn:
        post_id = save_post(conn, post)
        with conn:
            conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                         (str(today_kst()), post.category, post.request_id, 'DROP_REVIEW'))
        assert recover_rejected(settings, conn, [request])[0]['status'] == 'ERROR'
        result = recover_rejected(settings, conn, [request])[0]
        assert result['status'] == 'APPROVED' and result['id'] == post_id
        assert recover_rejected(settings, conn, [request]) == []
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 1
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 1
    assert calls == [False, True]


def test_cached_response_dict_preserves_observed_urls():
    from blogbot.llm import _extract_urls

    result = _extract_urls({'output': [
        {'status': 'completed', 'action': {'type': 'open_page', 'url': 'https://example.com/a'}},
        {'status': 'failed', 'action': {'type': 'open_page', 'url': 'https://example.com/b'}},
    ]})
    assert result == ['https://example.com/a']


@pytest.mark.parametrize('url,suffix', [
    ('https://www.healthychildren.org/English/safety-prevention/at-home/Pages/Make-Babys-Room-Safe.aspx', '?form=HealthyChildren'),
    ('https://acsm.org/resistance-training-guidelines-update-2026/', '?nocache=1775184380'),
])
def test_official_display_parameters_do_not_reject_completed_drafts(url, suffix):
    assert _source_urls({'source_urls': [url], 'body': url}, [url + suffix]) == [url]
    with pytest.raises(ValueError):
        _source_urls({'source_urls': [url + '?document=2']}, [url + '?document=1' + suffix[1:]])


def test_dart_receipt_numbers_remain_distinct():
    with pytest.raises(ValueError):
        _source_urls({'source_urls': ['https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260930801114']},
                     ['https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260930801115'])


@pytest.mark.parametrize('url,suffix', [
    ('https://www.healthychildren.org/english/ages-stages/baby/sleep/pages/a-parents-guide-to-safe-sleep.aspx', '?keyword=joint'),
    ('https://publications.aap.org/pediatrics/article/doi/10.1542/peds.2022-057990/188304/Sleep-Related-Infant-Deaths-Updated-2022', '?autologincheck=redirected'),
    ('https://www.mayoclinic.org/healthy-lifestyle/fitness/in-depth/weight-training/art-20045842', '?p=1&pg=1'),
])
def test_recovered_official_article_variants_preserve_document_identity(url, suffix):
    assert _source_urls({'source_urls': [url]}, [url + suffix]) == [url]
    with pytest.raises(ValueError):
        _source_urls({'source_urls': [url]}, [url + '?document=2'])


def test_aap_doi_and_volume_routes_keep_article_id():
    doi = 'https://publications.aap.org/pediatrics/article/doi/10.1542/peds.2022-057990/188304/Sleep-Related-Infant-Deaths-Updated-2022'
    volume = 'https://publications.aap.org/pediatrics/article/150/1/e2022057990/188304/Sleep-Related-Infant-Deaths-Updated-2022-Recommendations'
    assert _source_urls({'source_urls': [doi]}, [volume]) == [doi]
    with pytest.raises(ValueError):
        _source_urls({'source_urls': [doi]}, [volume.replace('188304', '188305')])


def test_source_error_recovery_reuses_completed_writer_without_repurchase(settings, monkeypatch):
    from blogbot.images import atomic_json
    from blogbot.recovery import rejected_checkpoint
    request = ContentRequest('cached-url-error', 'parenting', {'question': '안전 질문'})
    folder = settings.db_path.parent/'response-cache'
    folder.mkdir()
    url = 'https://www.healthychildren.org/English/safety-prevention/at-home/Pages/Make-Babys-Room-Safe.aspx'
    atomic_json(folder/f'{today_kst()}-completed.json', {
        'payload': {'title': '검증된 작성 응답', 'subcategory': '육아생활', 'body': '안전 본문',
                    'tags': ['안전'], 'source_urls': [url]},
        'response': {'id': 'completed-writer', 'output': [{'status': 'completed', 'action': {
            'type': 'search', 'sources': [{'url': url+'?form=HealthyChildren'}]}}]}})
    (settings.db_path.parent/'usage.jsonl').write_text(
        '{"request_id":"cached-url-error","stage":"writer","response_id":"completed-writer"}\n')
    with closing(connect_db(settings.db_path)) as conn:
        reserve_attempt(conn, settings.config, 3, [request])
        conn.execute("UPDATE attempts SET status='ERROR'")
        conn.commit()
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: ([request], []))
    monkeypatch.setattr('blogbot.pipeline.prepare_request', lambda _, r: r)
    monkeypatch.setattr('blogbot.pipeline.validate_structure', lambda *a: None)
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda s, r, p: p)
    class LLM:
        def __init__(self, *a): pass
        def create_draft(self, *a): pytest.fail('Writer already completed')
        def review(self, *a):
            return {'scores': [5]*6, 'total': 30, 'decision': 'PASS', 'issues': [],
                    'blocking_issues': [], 'rewrite_instructions': ''}
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', LLM)
    assert rejected_checkpoint(settings, request)['review'] == {}
    assert run_daily(settings, retry_failed=True)[0]['status'] == 'APPROVED'


def test_invalid_preview_recovery_rewrites_cached_draft_before_review(settings, monkeypatch):
    from dataclasses import asdict, replace
    request = ContentRequest('preview-repair', 'investment', {'kind': 'policy'})
    body = '작성일: 2026-10-02\n\n## 사실\n내용\n\n## 의미\n내용\n\n## 조건\n내용\n\n## 정리\n### 변수\n내용'
    post = PostDraft('investment', '시장·산업', '정책', '정책 확인', body, [],
                     ['https://example.com/policy'], str(today_kst()), request_id=request.id)
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), request.category, request.id, 'ERROR'))
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: ([request], []))
    monkeypatch.setattr('blogbot.pipeline.prepare_request', lambda s, r: r)
    monkeypatch.setattr('blogbot.recovery.rejected_checkpoint', lambda *a: {'post': asdict(post)})
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda s, r, p: p)
    calls = []
    class LLM:
        def __init__(self, *a): pass
        def create_draft(self, *a): pytest.fail('Completed writer must be reused')
        def rewrite(self, p, info, review, request):
            calls.append(review['rewrite_instructions'])
            return replace(p, body=p.body.replace('작성일: 2026-10-02', '핵심 정책 내용을 확인해요.'))
        def review(self, *a):
            return {'scores': [5]*6, 'total': 30, 'decision': 'PASS'}
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', LLM)
    assert run_daily(settings, retry_failed=True)[0]['status'] == 'APPROVED'
    assert calls == ['Start with a plain-language preview summary, not dates or URLs']


def test_recovery_does_not_repurchase_uncertain_failed_api(settings, monkeypatch):
    request = ContentRequest('uncertain-api', 'exercise', {'question': '자세'})
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), request.category, request.id, 'ERROR'))
    (settings.db_path.parent/'usage.jsonl').write_text(json.dumps({
        'request_id': request.id, 'stage': 'writer', 'error': 'APITimeoutError'}) + '\n')
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: ([request], []))
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', lambda *a: pytest.fail('Do not repurchase'))
    assert run_daily(settings, retry_failed=True)[0]['status'] == 'MANUAL_CHECK_REQUIRED'


def test_recovery_skips_failed_replacement_after_category_is_approved(settings, monkeypatch):
    request = ContentRequest('replacement-exercise', 'exercise', {'question': '자세'})
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), 'exercise', request.id, 'ERROR'))
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: ([request], []))
    monkeypatch.setattr('blogbot.pipeline.rank_candidates', lambda *a: [request])
    def repaired(s, conn, candidates):
        post = draft()
        post.category, post.request_id = 'exercise', 'original-exercise'
        save_post(conn, post)
        return [{'status': 'APPROVED', 'category': 'exercise'}]
    monkeypatch.setattr('blogbot.recovery.recover_rejected', repaired)
    monkeypatch.setattr('blogbot.pipeline.prepare_request',
                        lambda *a: pytest.fail('Approved category must not purchase another draft'))
    results = run_daily(settings, retry_failed=True)
    assert results[1]['status'] == 'RECOVERY_SUPERSEDED'
    with closing(connect_db(settings.db_path)) as conn:
        assert conn.execute('SELECT status FROM attempts').fetchone()[0] == 'SUPERSEDED'


@pytest.mark.parametrize('opened,status,accepted', [
    (False, 'SUPPORTED', False), (True, 'UNVERIFIED', False), (True, 'SUPPORTED', True),
])
def test_high_score_requires_actual_source_checks(monkeypatch, opened, status, accepted):
    from blogbot.llm import BlogLLM
    post = draft()
    post.category = 'parenting'
    llm = object.__new__(BlogLLM)
    llm.reviewer_prompt, llm.review_model, llm.client, llm.journal = '', 'test', None, None
    payload = {'scores': [5]*6, 'total': 30, 'decision': 'PASS', 'issues': [],
               'blocking_issues': [], 'rewrite_instructions': '', 'source_checks': [
                   {'claim': '검증 대상', 'evidence': '원문 근거',
                    'source_url': post.source_urls[0], 'status': status}]}
    response = {'output': [{'status': 'completed', 'action': {
        'type': 'open_page' if opened else 'search', 'url': post.source_urls[0]}}]}
    monkeypatch.setattr('blogbot.llm.request_json', lambda *a, **kw: (payload, response))
    result = llm.review(post, {}, ContentRequest('test', post.category, {}))
    assert (review_result(result)[1] == 'PASS') is accepted


@pytest.mark.parametrize('status,accepted', [('completed', True), ('searching', False)])
def test_completed_find_in_original_is_read_evidence(status, accepted):
    from blogbot.llm import _checked_review
    url = 'https://www.cdc.gov/reproductive-health/features/babies-sleep.html'
    payload = {'scores': [5]*6, 'total': 30, 'decision': 'PASS', 'issues': [],
               'blocking_issues': [], 'rewrite_instructions': '', 'source_checks': [
                   {'claim': '핵심 주장', 'source_url': url, 'evidence': '원문 일치',
                    'status': 'SUPPORTED'}]}
    response = {'output': [{'status': status, 'action': {
        'type': 'find_in_page', 'url': url, 'pattern': 'sleep'}}]}
    assert (review_result(_checked_review(payload, response,
        ContentRequest('test', 'parenting', {})))[1] == 'PASS') is accepted


def test_recovery_preserves_sources_observed_in_earlier_revisions(tmp_path):
    import json

    from blogbot.llm import _historical_source_urls
    cache = tmp_path / 'response-cache'
    cache.mkdir()
    journal = tmp_path / 'usage.jsonl'
    entries = [{'request_id': rid, 'stage': 'rewrite', 'response_id': ident}
               for rid, ident in [('same', 'older'), ('same', 'latest'), ('other', 'unrelated')]]
    journal.write_text('\n'.join(json.dumps(e) for e in entries))
    for e in entries:
        response = {'id': e['response_id'], 'output': [{'status': 'completed', 'action': {
            'type': 'open_page', 'url': 'https://example.com/' + e['response_id']}}]}
        (cache / f"{today_kst()}-{e['response_id']}.json").write_text(
            json.dumps({'response': response}))
    assert set(_historical_source_urls(journal, 'same')) == {
        'https://example.com/older', 'https://example.com/latest'}


def test_editorial_recovery_updates_checkpoint_and_caps_new_revisions(settings, monkeypatch):
    from dataclasses import asdict

    from blogbot.recovery import recover_rejected
    post = draft()
    post.category, post.subcategory, post.request_id = 'parenting', '육아생활', 'revision-limit'
    request = ContentRequest(post.request_id, post.category, {})
    monkeypatch.setattr('blogbot.recovery.prepare_request', lambda s, r: r)
    monkeypatch.setattr('blogbot.recovery.rejected_checkpoint', lambda *a: {
        'post': asdict(post), 'input': asdict(request), 'review': {'decision': 'REWRITE'}})
    monkeypatch.setattr('blogbot.recovery.validate_structure', lambda *a: None)
    calls = []
    class LLM:
        def __init__(self, *a): pass
        def rewrite(self, p, *a, cache_only=False):
            calls.append((p.body, cache_only))
            p.body += ' 수정'
            return p
        def review(self, *a):
            return {'scores': [4,5,5,4,5,5], 'total': 28, 'decision': 'REWRITE',
                    'issues': ['공식 근거로 수정 필요'], 'rewrite_instructions': '수정'}
    monkeypatch.setattr('blogbot.recovery.BlogLLM', LLM)
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), post.category, post.request_id, 'DROP_REVIEW'))
        assert recover_rejected(settings, conn, [request])[0]['status'] == 'DROP_REVIEW'
        assert recover_rejected(settings, conn, [request])[0]['status'] == 'DROP_REVIEW'
        assert recover_rejected(settings, conn, [request])[0]['reason'] == 'editorial_revision_limit'
    assert len(calls) == 2 and calls[1][0] == calls[0][0] + ' 수정'
    assert all(not cached for _, cached in calls)
