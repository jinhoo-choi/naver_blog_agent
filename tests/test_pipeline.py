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
    monkeypatch.setenv("BLOG_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NAVER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("NAVER_BLOG_ID", "example")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder")
    config = load_settings()
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


def test_recovery_reuses_error_reservation_without_consuming_new_input(settings, monkeypatch):
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
    assert len(result) == 1 and result[0]['request_id'] == 'failed'
    with closing(connect_db(settings.db_path)) as conn:
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


def test_cached_response_dict_preserves_observed_urls():
    from blogbot.llm import _extract_urls

    result = _extract_urls({'output': [
        {'status': 'completed', 'action': {'type': 'open_page', 'url': 'https://example.com/a'}},
        {'status': 'failed', 'action': {'type': 'open_page', 'url': 'https://example.com/b'}},
    ]})
    assert result == ['https://example.com/a']
