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
    record = {"id": "source-1", "kind": "policy", "facts": f"자료 기준일 {today}",
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


@pytest.mark.parametrize('kind', ['flow', 'theme'])
def test_price_and_theme_recaps_are_not_blog_inputs(kind):
    record, origin = investment_record()
    record['kind'] = kind
    assert community_request(record, origin, {}) is None


def investment_record():
    today = today_kst().isoformat()
    return ({'id': 'report-1', 'kind': 'research', 'stock_name': '예시기업',
             'stock_code': '123456', 'facts': f'발간일: {today}\n영업이익 전망 상향',
             'title': '실적 전망 변화', 'src': 'https://example.com/report/1',
             'body': '원본 사실 요약',
             'score': {'factual': 5, 'useful': 4, 'natural': 4, 'compliant': 5,
                       'gain': 4, 'fit': 4, 'fatal': []}},
            {'repository': 'owner/source', 'snapshot_date': today, 'commit': 'a' * 40})


@pytest.mark.parametrize('facts', [
    '시장 전망과 산업 리포트',
    '자료 요약\n※ 계약·수주·임상 이슈가 있으면 확인할 것',
])
def test_generic_words_and_instructions_do_not_establish_a_stock_issue(facts):
    record, origin = investment_record()
    record['facts'] = f'발간일: {today_kst()}\n{facts}'
    record['body'] = '계약 체결을 발표했다고 작성된 미검증 생성 문장'
    assert community_request(record, origin, {}) is None


def test_clear_stock_event_and_sector_policy_are_distinguished():
    record, origin = investment_record()
    assert community_request(record, origin, {}) is not None
    record.update(kind='policy', theme_assigned=True, board_mapping='SECTOR_PROXY',
                  facts=f'보도 시각: {today_kst()} 08:30 KST\n산업 지원 발표')
    request = community_request(record, origin, {})
    assert request.data['sector_only'] is True
    assert request.data['stock_name'] == request.data['stock_code'] == ''


@pytest.mark.parametrize('facts', [
    '발간일: 2020-01-01\n신규 계약 예정일: {today}',
    '날짜 미상\n영업이익 전망 상향',
])
def test_export_date_and_future_events_cannot_refresh_old_or_undated_sources(facts):
    record, origin = investment_record()
    record['facts'] = facts.format(today=today_kst())
    assert community_request(record, origin, {}) is None


def test_collect_waits_for_today_morning_export_without_consuming_inputs(tmp_path, monkeypatch):
    from datetime import timedelta

    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    settings = load_settings()
    record, origin = investment_record()
    yesterday = today_kst() - timedelta(days=1)
    origin.update(snapshot_date=str(yesterday), snapshot_at=f'{yesterday}T09:00:00+09:00')
    monkeypatch.setattr('blogbot.inputs.fetch_community', lambda _: ([record], origin))
    requests, notices = collect_requests(settings)
    assert requests == [] and notices == [{'status': 'COMMUNITY_SOURCE_PENDING'}]
    origin.update(snapshot_date=str(today_kst()), snapshot_at=f'{today_kst()}T08:30:00+09:00')
    assert len(collect_requests(settings)[0]) == 1


def test_snapshot_date_is_korean_date_not_utc(monkeypatch):
    import base64

    from blogbot.inputs import fetch_community

    responses = iter([
        [{'sha': 'a' * 40, 'commit': {'committer': {'date': '2026-09-28T23:35:00Z'}}}],
        {'encoding': 'base64', 'content': base64.b64encode(b'[]').decode()},
    ])
    monkeypatch.setattr('blogbot.inputs._get_json', lambda _: next(responses))
    _, origin = fetch_community({'repository': 'owner/source'})
    assert origin['snapshot_date'] == '2026-09-29'
    assert origin['snapshot_at'] == '2026-09-29T08:35:00+09:00'
