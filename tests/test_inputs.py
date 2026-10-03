import json

import pytest

from blogbot.config import load_settings
from blogbot.core import today_kst
from blogbot.inputs import collect_requests, community_request, enqueue_file


def test_cooking_needs_recipe_and_photo_and_tracks_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("BLOG_DATA_DIR", str(tmp_path / "data"))
    settings = load_settings()
    settings.config['topics'].pop('scheduled', None)
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


@pytest.mark.parametrize('date_line', [
    '공시일: {compact}', '발간일: {today}', '보도 시각: {today} 08:30 KST',
    '발간: 미래에셋증권 / {today}',
])
def test_real_export_date_formats_keep_the_same_source_date(date_line):
    record, origin = investment_record()
    today = today_kst().isoformat()
    record['facts'] = date_line.format(today=today, compact=today.replace('-', '')) + '\n영업이익 상향'
    assert community_request(record, origin, {}).data['source_date'] == today


def test_old_report_with_future_contract_date_is_still_rejected():
    record, origin = investment_record()
    record['facts'] = f'발간: 증권사 / 2020-01-01\n계약 종료일: {today_kst()}\n매출 증가'
    assert community_request(record, origin, {}) is None


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


def test_specific_ir_event_is_not_lost_without_literal_earnings_keyword():
    record, origin = investment_record()
    record['facts'] = f'발간: 증권사 / {today_kst()}\n대표이사 IR 간담회, 2030년 별도 OPM 8% 전망'
    request = community_request(record, origin, {})
    assert request is not None and request.data['kind'] == 'research'


@pytest.mark.parametrize('facts', [
    '발간일: 2020-01-01\n신규 계약 예정일: {today}',
    '날짜 미상\n영업이익 전망 상향',
])
def test_export_date_and_future_events_cannot_refresh_old_or_undated_sources(facts):
    record, origin = investment_record()
    record['facts'] = facts.format(today=today_kst())
    assert community_request(record, origin, {}) is None


def test_collect_waits_for_today_morning_export_without_consuming_inputs(
    tmp_path, monkeypatch, weekday_clock,
):
    from datetime import timedelta

    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    settings = load_settings()
    settings.config['topics'].pop('scheduled', None)
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


def test_dated_topic_replaces_only_selected_category_and_expires(tmp_path, monkeypatch):
    from datetime import date

    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    settings = load_settings()
    settings.config['community']['enabled'] = False
    for category in ['parenting', 'exercise']:
        source = tmp_path / f'{category}.json'
        source.write_text(json.dumps({'id': f'queued-{category}', 'category': category,
                                      'question': '기존 질문입니다'}))
        enqueue_file(settings, source)
    selected = {'id': 'selected-topic', 'category': 'parenting',
                'data': {'question': '선택한 질문입니다'}}
    settings.config['topics']['scheduled'] = {'2026-10-02': selected}
    monkeypatch.setattr('blogbot.inputs.today_kst', lambda: date(2026, 10, 1))
    assert {r.id for r in collect_requests(settings)[0]} == {
        'queued-parenting', 'queued-exercise'}
    monkeypatch.setattr('blogbot.inputs.today_kst', lambda: date(2026, 10, 2))
    assert [r.id for r in collect_requests(settings)[0]] == ['selected-topic', 'queued-exercise']
    assert [r.id for r in collect_requests(settings)[0]].count('selected-topic') == 1
    monkeypatch.setattr('blogbot.inputs.today_kst', lambda: date(2026, 10, 5))
    assert {r.id for r in collect_requests(settings)[0]} == {
        'queued-parenting', 'queued-exercise'}


@pytest.mark.parametrize('day,quotas,maximum', [
    ('2026-10-02', (1, 1, 1), 5),
    ('2026-10-03', (1, 0, 0), 1),
    ('2026-10-04', (1, 0, 0), 1),
    ('2026-10-05', (1, 1, 1), 5),
    ('2026-10-10', (1, 0, 0), 1),
])
def test_weekend_feature_quota_and_selected_topic(tmp_path, monkeypatch, day, quotas, maximum):
    from datetime import date

    today = date.fromisoformat(day)
    monkeypatch.setattr('blogbot.config.today_kst', lambda: today)
    monkeypatch.setattr('blogbot.inputs.today_kst', lambda: today)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    settings = load_settings()
    assert tuple(settings.config['categories'][c]['max_daily']
                 for c in ['parenting', 'exercise', 'investment']) == quotas
    assert settings.config['blog']['daily_max'] == maximum
    assert settings.daily_count == min(3, maximum)
    assert tuple(settings.config['images'][c + '_count']
                 for c in ['parenting', 'exercise', 'investment']) == (5, 5, 3)
    if today.weekday() >= 5:
        assert settings.config['community']['enabled'] is False
        assert any('실제 캡처' in rule for rule in settings.config['categories']['parenting']['rules'])
    if day == '2026-10-03':
        monkeypatch.setattr('blogbot.inputs.fetch_community',
                            lambda _: pytest.fail('Weekend must not fetch community'))
        requests, notices = collect_requests(settings)
        assert [r.id for r in requests] == ['owner-20261003-esl-feeding-feature']
        assert not notices
        context = requests[0].data['context']
        for detail in ['출생 재태주수가 아니라 평가 당시 월경후연령',
                       'ESL 자세 비교20명과 별도 paced bottle feeding 비교20명',
                       '각 조건2분씩', '저희 아이에게는 도움이 됐습니다',
                       '기침·사레가 적고 호흡 멈춤이 짧게 관찰',
                       '별도 paced bottle feeding군의 결과를 ESL 결과와 합치지 않는다',
                       'ESL 자세와 단계별 순서', '각각 독립 대제목',
                       '정확한 의료 자세·손 위치·순서',
                       '세이지 그린', '따뜻한 노란색', '응급 도움',
                       '임상 근거와 분리', '반복되는 기침·사레',
                       '단단하고 평평한 별도 수면 공간', '최종 총5장을 강제하지 않는다']:
            assert detail in context
        for url in ['https://pubmed.ncbi.nlm.nih.gov/39721201/',
                    ('https://www.ruh.nhs.uk/patients/patient_information/'
                     'NIC034_Elevated_Side_Lying_feeding.pdf'),
                    ('https://www.cuh.nhs.uk/patient-information/'
                     'supporting-safe-effective-and-enjoyable-bottle-feeding/'),
                    'https://safetosleep.nichd.nih.gov/reduce-risk/safe-sleep-environment']:
            assert url in context
        rules = '\n'.join(settings.config['categories']['parenting']['rules'])
        for requirement in ['실제 이미지2장 이상', '공식 직접 링크카드1개 이상',
                            '재사용 자료1개', '재사용 권한', '보조 삽화는 생략']:
            assert requirement in rules


def test_interest_prefers_eligible_stock_without_overriding_report_priority(monkeypatch):
    from types import SimpleNamespace

    from blogbot.inputs import ContentRequest
    from blogbot.topics import rank_candidates

    settings = SimpleNamespace(config={'topics': {'enabled': False},
                                      'community': {'search_interest_keywords': ['아톤']}})
    candidates = [ContentRequest('other-disclosure', 'investment',
                                 {'stock_name': '다른기업', 'kind': 'disclosure'}),
                  ContentRequest('parent', 'parenting', {'question': '육아 질문'}),
                  ContentRequest('aton-disclosure', 'investment',
                                 {'stock_name': '아톤', 'kind': 'disclosure'}),
                  ContentRequest('other-report', 'investment',
                                 {'stock_name': '다른기업', 'kind': 'research'}),
                  ContentRequest('aton-report', 'investment',
                                 {'stock_name': '아톤', 'kind': 'research'})]
    assert [r.id for r in rank_candidates(settings, None, candidates, 3)] == [
        'aton-report', 'parent', 'other-report', 'aton-disclosure', 'other-disclosure']
    settings.config['topics']['enabled'] = True
    monkeypatch.setattr('blogbot.topics._rank', lambda *args: list(reversed(candidates)))
    result = rank_candidates(settings, None, candidates, 3)
    assert [r.id for r in result if r.category == 'investment'] == [
        'aton-report', 'other-report', 'aton-disclosure', 'other-disclosure']
    assert len(result) == len(candidates)


def test_investment_benchmark_uses_actual_stock():
    from blogbot.inputs import ContentRequest
    from blogbot.research import public_query

    assert public_query(ContentRequest('aton', 'investment',
                        {'stock_name': '아톤', 'kind': 'disclosure'})) == '아톤 공시 분석'
