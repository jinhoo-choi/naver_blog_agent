import json

import pytest

from blogbot.config import load_settings
from blogbot.core import today_kst
from blogbot.inputs import collect_requests, community_request, enqueue_file


def test_cooking_needs_recipe_and_photo_and_tracks_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOG_DATA_DIR", str(tmp_path / "data"))
    settings = load_settings()
    settings.config["community"]["enabled"] = False
    source = tmp_path / "recipe.json"
    raw = {"id": "recipe-1", "category": "cooking",
           "recipe": {"name": "계란밥", "ingredients": ["계란 1개"], "steps": ["충분히 익힙니다."]}}
    source.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="supplied photos"):
        enqueue_file(settings, source)
    photo = tmp_path / "dish.png"
    photo.write_bytes(b"fixture-image-bytes")
    raw["photos"] = [{"file": "dish.png", "caption": "완성 사진"}]
    source.write_text(json.dumps(raw), encoding="utf-8")
    enqueue_file(settings, source)
    assert len(collect_requests(settings)[0]) == 1
    owned = settings.inbox_dir / "recipe-1" / "photo-01.png"
    owned.write_bytes(b"changed")
    requests, notices = collect_requests(settings)
    assert requests == []
    assert notices == [{"status": "INPUT_REJECTED"}]


def test_community_source_is_filtered_and_deduplicable_across_commits():
    today = today_kst().isoformat()
    record = {"id": "source-1", "kind": "flow", "facts": f"자료 기준일 {today}",
              "src": "https://finance.naver.com/", "body": "원본 사실 요약",
              "assignee": "must-not-leave-upstream", "raw_tail": "private diagnostic",
              "score": {"factual": 5, "useful": 4, "natural": 4, "compliant": 5,
                        "gain": 4, "fit": 4, "fatal": []}}
    origin = {"repository": "jinhoo-choi/kis-community-bot", "snapshot_date": today,
              "commit": "a" * 40}
    first = community_request(record, origin, {})
    second = community_request(record, {**origin, "commit": "b" * 40}, {})
    assert first.id == second.id
    assert "assignee" not in first.data and "raw_tail" not in first.data
    record["score"]["fatal"] = ["unsupported claim"]
    assert community_request(record, origin, {}) is None
    record["score"]["fatal"] = []
    record["score"]["factual"] = 3
    assert community_request(record, origin, {}) is None
    record["score"]["factual"] = 5
    assert community_request(record, {**origin, "snapshot_date": "2020-01-01"}, {}) is None
