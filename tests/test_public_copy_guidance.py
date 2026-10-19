"""Offline prompt/field boundaries, not a claim of generated-text quality."""
from copy import deepcopy
from pathlib import Path

import pytest

from blogbot.core import PostDraft, render_post_text, review_result
from blogbot.editorial import IMAGES, PUBLIC_COPY, quality_guidance
from blogbot.inputs import ContentRequest
from blogbot.llm import BlogLLM, _checked_review
from blogbot.pre_review import DraftCandidate
from blogbot.presentation import render_segments
from blogbot.responses import DRAFT_SCHEMA, REVIEW_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://example.org/public-copy-fixture'


def supported_review():
    return {'scores': [4] * 6, 'total': 24, 'decision': 'PASS',
            'issues': ['운영 참고: 미확인 비핵심 수치는 원고에서 제외됨'],
            'blocking_issues': [], 'rewrite_instructions': '',
            'source_checks': [{'claim': '공개 주장', 'source_url': SOURCE,
                               'evidence': '확인한 원문', 'status': 'SUPPORTED'}]}


@pytest.mark.parametrize('category,subtype,style', [
    ('parenting', 'article', 'article'), ('exercise', 'article', 'article'),
    ('investment', 'article', 'article'), ('cooking', 'article', 'article'),
    ('origins', 'article', 'article'), ('parenting', 'ai_tutorial', 'article'),
    ('parenting', 'article', 'review'), ('exercise', 'article', 'review'),
    ('cooking', 'article', 'review'),
])
def test_every_model_path_gets_public_private_contract_without_extra_calls(
        monkeypatch, category, subtype, style):
    calls = []
    draft = {'title': '제목', 'subcategory': 'fixture', 'body': '확인된 설명입니다.',
             'tags': ['설명'], 'source_urls': [] if category == 'cooking' else [SOURCE],
             'as_of_date': '2026-10-06'}
    response = {'output': [{'status': 'completed', 'action': {
        'type': 'open_page', 'url': SOURCE}}]}

    def respond(*args, **kwargs):
        calls.append(kwargs)
        return deepcopy(supported_review() if kwargs['stage'] == 'reviewer' else draft), response

    monkeypatch.setattr('blogbot.llm.request_json', respond)
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)
    client = BlogLLM.__new__(BlogLLM)
    client.client = client.journal = None
    client.model = client.review_model = 'offline'
    client.writer_prompt = (ROOT / 'prompts/writer.md').read_text()
    client.reviewer_prompt = (ROOT / 'prompts/reviewer.md').read_text()
    request = ContentRequest('public-copy-fixture', category, {
        'question': '확인된 질문', 'editorial_type': subtype, 'content_style': style})
    info = {'display_name': category, 'subcategories': ['fixture']}
    post = client.create_draft(request, info, [])
    review = client.review(post, info, request)
    client.rewrite(post, info, review, request)
    client.correct_draft(DraftCandidate(draft, draft['source_urls']), info, [], request)
    assert [c['stage'] for c in calls] == [
        'writer', 'reviewer', 'rewrite', 'pre_review_correction']
    for call in calls:
        assert '일반 정보 글은 썸네일 외 본문 설명 삽화 최소2장' not in call['input']
        assert '정확히 5개 고른다' not in call['input']
        assert '서로 다른 태그 5개인지' not in call['input']
        assert PUBLIC_COPY in call['input']
        assert '운영 참고: 태그 국소 보완:' in call['input']
        assert 'rewrite_instructions는 빈 문자열로 둔다' in call['input']
        assert '수정 필요 issues가 있으면 PASS하지 않는다' not in call['input']
        assert '최소 5개·최대 8개' in call['input']
        assert '상위 3~5개' in call['input']
        for obsolete in ['보통 3~5개', 'weekly-2221-v1 운영', '월·토는 육아',
                         '화·일은 이름의 유래', '도치 0~2회', '볼드 0~3곳', '유머 0~1문장']:
            assert obsolete not in call['input']
        assert quality_guidance(category, subtype, style) in call['input']
        assert 'concise-owner-v2' in call['input']
        assert '개요·설명/예시' in call['input']
        assert '실제 제공 경험·공식 사실·저자 분석' in call['input']
        assert IMAGES in call['input']
        assert call['model'] == 'offline'
        assert call['schema'] == (REVIEW_SCHEMA if call['stage'] == 'reviewer' else DRAFT_SCHEMA)
    assert 'tools' not in calls[-1]
    if category == 'cooking':
        assert all('tools' not in c for c in calls)
    else:
        assert all(c['max_tool_calls'] == 3 for c in calls[:3])
    if category == 'origins':
        for call in calls:
            assert '350~600' in call['input']
            assert '대제목 ## 최소 4개' not in call['input']
            assert 'topic-quality-v1' not in call['input']
    assert review_result(review) == (24, 'PASS')


def test_private_reference_does_not_hide_real_corrections_or_source_blocks():
    review = supported_review()
    assert review_result(review) == (24, 'PASS')
    review['issues'].append('본문에 중요한 계약 조건이 빠져 있습니다.')
    review['rewrite_instructions'] = '해당 계약 문장에 지급 조건을 복원하세요.'
    assert review_result(review) == (24, 'REWRITE')

    request = ContentRequest('review-fixture', 'investment', {})
    review = supported_review()
    # Search-only/no opened page cannot be called source-verified.
    checked = _checked_review(review, {'output': []}, request)
    assert checked['blocking_issues'] and review_result(checked) == (24, 'REWRITE')
    review = supported_review()
    review['source_checks'][0]['status'] = 'UNVERIFIED'
    response = {'output': [{'status': 'completed', 'action': {
        'type': 'open_page', 'url': SOURCE}}]}
    assert review_result(_checked_review(review, response, request)) == (24, 'REWRITE')
    review = supported_review()
    review['blocking_issues'] = ['필수 중단 신호 누락']
    assert review_result(review) == (24, 'REWRITE')


def test_public_render_does_not_append_private_review_or_as_of_metadata():
    marker = 'PRIVATE_QA_NOTE_DO_NOT_PUBLISH'
    post = PostDraft('investment', '기업·종목', 'fixture', '제목',
                     '조건 달성 시 최대 금액입니다.\n\n## 다음 일정\n\n공식 일정입니다.',
                     ['계약'], [SOURCE], '2026-10-06', provenance={
                         'review': {'issues': [marker], 'source_checks': [{'evidence': marker}],
                                    'rewrite_instructions': marker, 'blocking_issues': [marker]},
                         'omissions': [marker], 'source_date': '2026-10-05'})
    for text in [render_post_text(post), ''.join(s.html for s in render_segments(post))]:
        assert marker not in text and '2026-10-06' not in text
        assert '조건 달성 시 최대 금액' in text and SOURCE in text and '#계약' in text


def test_existing_schema_and_essential_conditions_are_retained():
    assert set(DRAFT_SCHEMA['properties']) == {
        'title', 'subcategory', 'body', 'tags', 'source_urls', 'as_of_date'}
    assert set(REVIEW_SCHEMA['properties']) == {
        'scores', 'total', 'decision', 'issues', 'blocking_issues',
        'rewrite_instructions', 'source_checks'}
    for term in ['수유 중단·응급 대응·안전수면', '단위·기간·조건·불확실성',
                 '중요한 계약·금융 조건', '광고·제휴 고지', 'blocking_issues', 'source_checks',
                 '실제사용본/미검증 제안본']:
        assert term in PUBLIC_COPY
    for name in ['writer', 'reviewer']:
        prompt = (ROOT / f'prompts/{name}.md').read_text()
        for term in ['비공개', '해당 사실 문장', '반복적 일반 경고', '원문']:
            assert term in prompt
        assert '한계를 같은 구역에서 한 번' not in prompt


def tag_review_path(monkeypatch, tags, result):
    """Run production writer/reviewer builders, source checks and final score handling."""
    import tomllib

    from blogbot.core import today_kst

    calls = []
    info = tomllib.loads((ROOT / 'config/blog.toml').read_text())['categories']['parenting']
    draft = {'title': '아기 목욕 준비와 순서', 'subcategory': info['subcategories'][0],
             'body': '아기목욕 준비물과 목욕물온도를 확인합니다.\n\n## 순서\n\n'
                     '목욕순서, 목욕후보습, 아기욕조, 아기수건, 목욕안전을 확인합니다.',
             'tags': tags, 'source_urls': [SOURCE], 'as_of_date': str(today_kst())}
    response = {'output': [{'status': 'completed', 'action': {
        'type': 'open_page', 'url': SOURCE}}]}

    def respond(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs['stage'] in {'writer', 'reviewer'}  # No correction/rewrite call.
        return deepcopy(result if kwargs['stage'] == 'reviewer' else draft), response

    monkeypatch.setattr('blogbot.llm.request_json', respond)
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)
    client = BlogLLM.__new__(BlogLLM)
    client.client = client.journal = None
    client.model = client.review_model = 'offline'
    client.writer_prompt = (ROOT / 'prompts/writer.md').read_text()
    client.reviewer_prompt = (ROOT / 'prompts/reviewer.md').read_text()
    request = ContentRequest('tag-local-review', 'parenting', {
        'question': '아기 목욕 준비와 순서는?', 'context': ''}, provenance={'source': 'owner_input'})
    post = client.create_draft(request, info, [])
    checked = client.review(post, info, request)
    prompt = calls[-1]['input']
    assert [call['stage'] for call in calls] == ['writer', 'reviewer']
    assert 'rewrite_instructions는 빈 문자열로 두고 합격 본문의 PASS를 유지한다' in prompt
    assert '태그만의 국소 편집' in prompt and '최소 5개·최대 8개' in prompt
    assert '중복·유사어 나열, 이웃모집/맞팔' not in prompt  # Removed contradictory instruction.
    assert checked['source_checks'] == result['source_checks']
    return post, checked, review_result(checked), prompt


@pytest.mark.parametrize('edit', [
    '중복 태그를 제거하고 관련 태그만 보충합니다.',
    '#·공백 표기를 정리합니다.',
    '같은 뜻의 유사어 태그를 하나로 정리합니다.',
])
@pytest.mark.parametrize('echo_instructions', [False, True])
def test_tag_local_edit_keeps_passing_text_after_real_review_processing(
        monkeypatch, edit, echo_instructions):
    from blogbot.editorial import TAG_EDIT_PREFIX

    note = TAG_EDIT_PREFIX + ' ' + edit
    result = supported_review()
    result.update(issues=[note], rewrite_instructions=note if echo_instructions else '')
    original = deepcopy(result)
    _, checked, decision, _ = tag_review_path(monkeypatch,
        ['아기목욕', '목욕준비물', '목욕물온도', '목욕순서', '목욕후보습', '#아기목욕'], result)
    assert decision == (24, 'PASS')
    assert checked['issues'] == [note] and not checked['blocking_issues']
    assert result == original  # Keep raw feedback/evidence; never mutate it to hide a defect.


@pytest.mark.parametrize('count', [4, 5, 6, 7, 8, 9])
def test_tag_count_and_duplicate_deficit_reach_review_result_without_rewrite(monkeypatch, count):
    from blogbot.editorial import TAG_EDIT_PREFIX

    tags = ['아기목욕', '목욕준비물', '목욕물온도', '목욕순서', '목욕후보습',
            '아기욕조', '아기수건', '목욕안전', '피부관리'][:count]
    result = supported_review()
    result['issues'] = ([TAG_EDIT_PREFIX + ' 중복 제거 후 최소 5개 미충족. 관련 태그만 보충 필요.']
                        if count < 5 else [])
    post, checked, decision, prompt = tag_review_path(monkeypatch, tags + [tags[0]], result)
    assert len(post.tags) == min(count, 8)
    assert decision == (24, 'PASS') and checked['rewrite_instructions'] == ''
    assert ('최소 5개 미충족' in prompt) == (count < 5)
    if count < 5:
        assert '관련 태그만 보충 필요' in checked['issues'][0]


@pytest.mark.parametrize('problem', ['fact', 'body', 'safety', 'privacy', 'source'])
def test_tag_notes_cannot_hide_real_body_safety_or_source_errors(monkeypatch, problem):
    from blogbot.editorial import TAG_EDIT_PREFIX

    note = TAG_EDIT_PREFIX + ' 중복 태그만 정리합니다.'
    result = supported_review()
    result.update(issues=[note], rewrite_instructions=note)
    if problem == 'fact':
        result['issues'].append('본문의 월령별 적용 조건에 실제 사실 오류가 있습니다.')
        result['rewrite_instructions'] = '본문의 잘못된 조건을 원문에 맞게 수정하세요.'
    elif problem == 'body':
        result['issues'].append('본문에서 같은 답을 반복하고 필요한 순서를 설명하지 않습니다.')
    elif problem == 'safety':
        result['scores'] = [5, 4, 4, 4, 4, 3]
    elif problem == 'privacy':
        result['blocking_issues'] = ['제공되지 않은 아동의 사적 정보를 노출했습니다.']
    else:
        result['source_checks'][0]['status'] = 'UNVERIFIED'
    _, _, decision, _ = tag_review_path(monkeypatch, ['아기목욕'], result)
    assert decision == (24, 'REWRITE')


@pytest.mark.parametrize('instruction', [
    '중복 태그 정리와 함께 본문의 사실 오류를 수정하세요.',
    '본문의 누락된 중단 신호를 추가하세요.',
    '운영 참고: 태그 국소 보완: 중복 정리\n본문의 계약 조건을 수정하세요.',
])
def test_tag_words_or_partial_markers_cannot_bypass_rewrite_guard(monkeypatch, instruction):
    from blogbot.editorial import TAG_EDIT_PREFIX

    result = supported_review()
    result.update(issues=[TAG_EDIT_PREFIX + ' 중복 정리'], rewrite_instructions=instruction)
    assert tag_review_path(monkeypatch, ['아기목욕'], result)[2] == (24, 'REWRITE')


@pytest.mark.parametrize('decision', ['REWRITE', 'DROP'])
def test_local_tag_marker_does_not_promote_a_nonpassing_review(monkeypatch, decision):
    from blogbot.editorial import TAG_EDIT_PREFIX

    result = supported_review()
    result.update(decision=decision, issues=[TAG_EDIT_PREFIX + ' 태그 정리'],
                  rewrite_instructions=TAG_EDIT_PREFIX + ' 태그 정리')
    assert tag_review_path(monkeypatch, ['아기목욕'], result)[2] == (24, decision)


def test_multiline_issue_cannot_hide_body_correction_in_a_tag_note(monkeypatch):
    from blogbot.editorial import TAG_EDIT_PREFIX

    result = supported_review()
    result.update(issues=[TAG_EDIT_PREFIX + ' 중복 정리\n본문의 실제 사실 오류도 수정 필요'],
                  rewrite_instructions=TAG_EDIT_PREFIX + ' 태그 정리')
    assert tag_review_path(monkeypatch, ['아기목욕'], result)[2] == (24, 'REWRITE')


def test_unscoped_old_tag_instruction_is_not_silently_reclassified():
    result = supported_review()
    result.update(issues=['중복 태그 정리가 필요합니다.'],
                  rewrite_instructions='중복 태그 정리와 본문 수정 여부를 확인하세요.')
    assert review_result(result) == (24, 'REWRITE')
