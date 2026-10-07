"""Offline prompt/field boundaries, not a claim of generated-text quality."""
from copy import deepcopy
from pathlib import Path

import pytest

from blogbot.core import PostDraft, render_post_text, review_result
from blogbot.editorial import PUBLIC_COPY, quality_guidance
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
        assert PUBLIC_COPY in call['input']
        assert quality_guidance(category, subtype, style) in call['input']
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
