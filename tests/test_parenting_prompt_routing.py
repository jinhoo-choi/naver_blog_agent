"""Capture real model inputs offline; selection is not permission to publish."""
import copy
import json
from pathlib import Path

import pytest
from test_strategy_reset import settings_at

from blogbot.core import review_result
from blogbot.editorial import IMAGES, PUBLIC_COPY, VOICE
from blogbot.inputs import ContentRequest, enqueue_file
from blogbot.llm import BlogLLM, _checked_review
from blogbot.pre_review import DraftCandidate, inspect_candidate

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://example.org/official-fixture'
QUESTION = '아기 월령별 씻기는 방법은 무엇인가요?'
AUTONOMOUS = {'source': 'work_autonomous', 'strategy_version': 'strategy-reset-v2'}


def capture_prompts(monkeypatch, settings, provenance, kind='article'):
    """Exercise all four production prompt builders, replacing only I/O."""
    from blogbot import llm

    calls = []
    payload = {'title': '아기 씻기는 방법', 'subcategory': '육아생활',
               'body': '적용 조건을 확인합니다.\n\n## 안전 조건\n\n공식 안내를 따릅니다.',
               'tags': ['아기목욕'], 'source_urls': [SOURCE], 'as_of_date': '2026-10-24',
               'scores': [4] * 6, 'total': 24, 'decision': 'PASS', 'issues': [],
               'blocking_issues': [], 'rewrite_instructions': '', 'source_checks': [
                   {'claim': '오프라인 예시', 'source_url': SOURCE,
                    'evidence': '검수 연결용 예시', 'status': 'SUPPORTED'}]}
    response = {'output': [{'status': 'completed', 'action': {
        'type': 'open_page', 'url': SOURCE}}]}
    monkeypatch.setattr(llm, 'request_json',
                        lambda *a, **kw: calls.append(kw) or (dict(payload), response))
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda *a: a[1])
    client = BlogLLM.__new__(BlogLLM)
    client.client = client.journal = None
    client.model = client.review_model = 'offline-test'
    client.writer_prompt = (ROOT / 'prompts/writer.md').read_text()
    client.reviewer_prompt = (ROOT / 'prompts/reviewer.md').read_text()
    data = {'question': QUESTION, 'age_months': None, 'context': ''}
    if kind == 'ai_tutorial':
        data['editorial_type'] = kind
    if kind == 'review':
        data['content_style'] = kind
    request = ContentRequest('prompt-fixture', 'parenting', data,
                             provenance={**provenance, 'daily_plan': settings.config['daily_plan']})
    info = settings.config['categories']['parenting']
    original = copy.deepcopy(info)
    post = client.create_draft(request, info, [])
    review = client.review(post, info, request)
    client.rewrite(post, info, review, request)
    client.correct_draft(DraftCandidate(dict(payload), [SOURCE]), info, [], request)
    assert info == original  # Routing must never leak one request's mode to another.
    return calls, request


@pytest.mark.parametrize('kind', ['article', 'ai_tutorial', 'review'])
@pytest.mark.parametrize('provenance,autonomous', [
    ({}, False),
    ({'source': 'owner_input'}, False),
    (AUTONOMOUS, True),
    ({'source': 'work_autonomous', 'strategy_version': 'strategy-reset-v1'}, False),
    ({'source': 'work_autonomous'}, False),
    ({'strategy_version': 'strategy-reset-v2'}, False),
    ({'source': 'unrecognized', 'strategy_version': 'strategy-reset-v2'}, False),
])
def test_final_parenting_prompts_route_selection_and_preserve_safety(
        tmp_path, monkeypatch, provenance, autonomous, kind):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    calls, request = capture_prompts(monkeypatch, settings, provenance, kind)
    assert [call['stage'] for call in calls] == [
        'writer', 'reviewer', 'rewrite', 'pre_review_correction']
    for call in calls:
        prompt = call['input']
        # A conflict resolution must retain latest main's editor/publishing contract
        # on every production path and for every provenance/style combination.
        for guidance in [PUBLIC_COPY, VOICE, IMAGES]:
            assert guidance in prompt
        for rule in ['native-editor-features-v1', 'KRX 카드와 NXT 반영 종가',
                     '같은 대상의 글감·URL 카드·주소를 중복 나열하지 않는다',
                     '소유자가 직접 발행한다', '별도 재평가 루프·실제 발행을 추가하지 않으며',
                     'concise-owner-v2', '모든 글에 일괄 강제하지 않는다']:
            assert rule in prompt
        assert QUESTION in prompt
        for obsolete in ['weekly-2221-v1 운영', '월·토는 육아', '화·일은 이름의 유래',
                         '0~2회', '0~3곳', '0~1문장', '보통 3~5개']:
            assert obsolete not in prompt
        assert '최소 5개·최대 8개' in prompt
        assert '선우와 관련해 사용자가 실제로 입력한 질문에만 답한다. 자율 주제 선정 금지.' not in prompt
        if autonomous:
            for term in ['자율 제작:', 'Creator Advisor', '네이버 검색', '중복',
                         '사용자 직접 질문이 아니라는 이유만으로 배제하지 않는다']:
                assert term in prompt
            assert '수동 제작:' not in prompt
            assert '사용자가 고른 실제 질문' not in prompt
        else:
            assert '수동 제작: 사용자가 실제로 입력한 질문에만 답한다' in prompt
            assert '자율 제작:' not in prompt
        for rule in ['개별 아동에 대한 진단이나 치료 지시를 하지 않는다',
                     '공공기관·학회·병원', '사적 정보는 원고에 노출하지 않는다',
                     '체험담을 창작하지 않는다', '수유 중단·응급 대응·안전수면']:
            assert rule in prompt
        assert '월령' in prompt and '경험' in prompt
        assert request.data['age_months'] is None and request.data['context'] == ''
    assert all(call['max_tool_calls'] == 3 for call in calls[:3])
    assert 'tools' not in calls[-1] and calls[-1]['max_output_tokens'] == 12000
    assert '정확성 또는 안전규칙 점수가 4 미만이면' in calls[1]['input']


@pytest.mark.parametrize('source', ['owner_input', 'work_autonomous'])
def test_registered_parenting_question_keeps_actual_origin(tmp_path, monkeypatch, source):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    file = tmp_path / 'request.json'
    file.write_text(json.dumps({'id': 'registered', 'category': 'parenting',
        'question': QUESTION, 'source': source, 'strategy_version': 'strategy-reset-v2'}))
    enqueue_file(settings, file)
    packet = json.loads((settings.inbox_dir / 'registered/request.json').read_text())
    assert packet['provenance']['source'] == source
    assert packet['data']['question'] == QUESTION
    assert packet['data']['age_months'] is None and packet['data']['context'] == ''


@pytest.mark.parametrize('day,version', [
    ('2026-10-24', 'strategy-reset-v1'), ('2026-10-25', 'strategy-reset-v2'),
])
def test_autonomy_still_requires_current_approved_slot(tmp_path, monkeypatch, day, version):
    settings = settings_at(tmp_path, monkeypatch, day)
    file = tmp_path / 'request.json'
    file.write_text(json.dumps({'id': 'unapproved', 'category': 'parenting',
        'question': QUESTION, 'source': 'work_autonomous', 'strategy_version': version}))
    with pytest.raises(ValueError, match='current approved Work slot'):
        enqueue_file(settings, file)
    assert not (settings.inbox_dir / 'unapproved').exists()


@pytest.mark.parametrize('body,code', [
    ('저는 이 방법을 해봤더니 편했습니다.', 'unsupported_personal_experience'),
    ('선우는 4개월입니다.', 'unsupported_child_age'),
])
def test_autonomous_selection_cannot_invent_experience_or_age(tmp_path, monkeypatch, body, code):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    request = ContentRequest('fabricated', 'parenting', {'question': QUESTION},
                             provenance=AUTONOMOUS)
    candidate = DraftCandidate({'title': '아기 목욕', 'subcategory': '육아생활',
        'body': body + '\n\n## 조건\n\n공식 조건입니다.', 'tags': ['아기목욕'],
        'source_urls': [SOURCE], 'as_of_date': '2026-10-24'}, [SOURCE])
    post, codes, _ = inspect_candidate(candidate, request, settings.config['categories']['parenting'])
    assert post is not None and code in codes  # Any code holds the pre-review candidate.


@pytest.mark.parametrize('index', [0, 5])
def test_autonomous_review_cannot_waive_accuracy_or_safety(index):
    scores = [5] * 6
    scores[index] = 3
    _, decision = review_result({'scores': scores, 'total': sum(scores), 'decision': 'PASS',
        'issues': [], 'blocking_issues': [], 'rewrite_instructions': ''})
    assert decision != 'PASS'


def test_autonomous_review_still_requires_read_source_evidence():
    request = ContentRequest('no-evidence', 'parenting', {'question': QUESTION},
                             provenance=AUTONOMOUS)
    review = _checked_review({'decision': 'PASS', 'source_checks': [
        {'claim': '확인되지 않은 주장', 'source_url': SOURCE,
         'evidence': '검색 요약', 'status': 'SUPPORTED'}]}, {'output': []}, request)
    assert review['decision'] == 'REWRITE' and review['blocking_issues']
