from contextlib import closing

import pytest

from blogbot.config import load_settings
from blogbot.core import (
    PostDraft, connect_db, reserve_attempt, review_result, save_post, today_kst,
)
from blogbot.llm import _source_urls
from blogbot.pipeline import run_daily, save_pending


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOG_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("NAVER_PROFILE_DIR", str(tmp_path / "profile"))
    monkeypatch.setenv("NAVER_BLOG_ID", "example")
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder")
    return load_settings()


def draft():
    return PostDraft(
        "cooking", "집밥", "국 끓이는 순서", "국 끓이는 순서와 재료 준비",
        "재료를 계량하고 충분히 가열합니다.", ["집밥"],
        ["https://www.foodsafetykorea.go.kr/"], today_kst().isoformat(),
        27, "APPROVED",
    )


def test_quota_survives_connection_restart(settings):
    for _ in range(2):
        with closing(connect_db(settings.db_path)) as conn:
            assert reserve_attempt(conn, settings.config, 2)
    with closing(connect_db(settings.db_path)) as conn:
        assert reserve_attempt(conn, settings.config, 2) is None


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

        def create_draft(self, category, info, recent):
            post = draft()
            post.category, post.subcategory = category, info["subcategories"][0]
            post.title = "완전히 다른 주제의 작성 후보"
            return post

        def review(self, post, info):
            self.reviews += 1
            scores = [4, 3, 3, 3, 3, 4] if self.reviews == 1 else [5] * 6
            return {"scores": scores, "total": sum(scores),
                    "decision": "REWRITE" if self.reviews == 1 else "PASS"}

        def rewrite(self, post, info, review):
            post.title = first.title
            return post

    monkeypatch.setattr("blogbot.pipeline.BlogLLM", FakeLLM)
    assert run_daily(settings, count=1)[0]["status"] == "DROP_DUPLICATE"


def test_three_candidates_cover_all_categories(settings):
    with closing(connect_db(settings.db_path)) as conn:
        categories = [reserve_attempt(conn, settings.config, 3)[1] for _ in range(3)]
    assert set(categories) == {"parenting", "investment", "cooking"}
