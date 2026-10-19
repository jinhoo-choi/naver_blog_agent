"""Offline checks of the actual model inputs against the current operating plan."""
from datetime import date
from pathlib import Path

import pytest

from blogbot.config import load_settings
from blogbot.inputs import ContentRequest
from blogbot.llm import BlogLLM
from blogbot.pre_review import DraftCandidate

ROOT = Path(__file__).resolve().parents[1]


def assert_daily_prompt(prompt, config=None):
    assert 'daily_plan' in prompt
    for obsolete in ['weekly-2221-v1 운영', '월·토는 육아', '화·일은 이름의 유래',
                     '도치 0~2회', '볼드 0~3곳', '유머 0~1문장', '보통 3~5개']:
        assert obsolete not in prompt
    assert '최소 5개·최대 8개' in prompt
    if config:
        assert config['daily_plan']['version'] in prompt
        assert '모든 글에 일괄 강제하지 않는다' in prompt


@pytest.mark.parametrize('filename', ['writer.md', 'reviewer.md'])
def test_prompt_files_use_current_plan_without_retired_instructions(filename):
    assert_daily_prompt((ROOT / 'prompts' / filename).read_text())


@pytest.mark.parametrize('day,category,subtype', [
    ('2026-10-24', 'parenting', 'article'),
    ('2026-10-23', 'investment', 'article'),
    ('2026-10-31', 'parenting', 'ai_tutorial'),
])
def test_all_model_paths_receive_consistent_daily_plan(
        tmp_path, monkeypatch, day, category, subtype):
    from blogbot import llm

    current = date.fromisoformat(day)
    for module in ['config', 'planning', 'llm']:
        monkeypatch.setattr(f'blogbot.{module}.today_kst', lambda: current)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('BLOG_DAILY_COUNT', '3')
    settings = load_settings()
    plan = settings.config['daily_plan']
    assert plan['category'] == category and plan['target'] == settings.daily_count == 1
    assert plan['depth'] == 'deep' and subtype in plan['editorial_types']
    captured = []
    source = 'https://example.org/offline-source'
    payload = {'title': '제목', 'subcategory': 'fixture', 'body': '확인한 답변입니다.',
               'tags': ['설명'], 'source_urls': [source], 'as_of_date': day,
               'scores': [4] * 6, 'total': 24, 'decision': 'PASS', 'issues': [],
               'blocking_issues': [], 'rewrite_instructions': '', 'source_checks': [
                   {'claim': 'fixture', 'source_url': source, 'evidence': 'fixture',
                    'status': 'SUPPORTED'}]}
    response = {'output': [{'status': 'completed', 'action': {
        'type': 'open_page', 'url': source}}]}
    monkeypatch.setattr(llm, 'request_json',
                        lambda *a, **kw: captured.append(kw) or (dict(payload), response))
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)
    client = BlogLLM.__new__(BlogLLM)
    client.client, client.journal = None, None
    client.model = client.review_model = 'offline'
    client.writer_prompt = (ROOT / 'prompts/writer.md').read_text()
    client.reviewer_prompt = (ROOT / 'prompts/reviewer.md').read_text()
    request = ContentRequest('offline-plan', category, {
        'question': '실제 제공된 질문', 'editorial_type': subtype},
        provenance={'daily_plan': plan, 'editorial_type': subtype,
                    **({'investment_mode': plan['investment_mode'],
                        'life_economics_checks': [{'url': source, 'verified_date': day}]}
                       if plan.get('investment_mode') else {})})
    info = settings.config['categories'][category]
    post = client.create_draft(request, info, [])
    review = client.review(post, info, request)
    client.rewrite(post, info, review, request)
    client.correct_draft(DraftCandidate(dict(payload), [source]), info, [], request)
    assert [c['stage'] for c in captured] == [
        'writer', 'reviewer', 'rewrite', 'pre_review_correction']
    for call in captured:
        assert_daily_prompt(call['input'], settings.config)
        assert '소유자가 직접 발행한다' in call['input']
        assert '"daily_plan"' in call['input']
        assert f'"category": "{category}"' in call['input']
    assert all(c['max_tool_calls'] == 3 for c in captured[:3])
    assert 'tools' not in captured[-1] and captured[-1]['max_output_tokens'] == 12000
    assert '총점 30점 중 24점 이상 PASS' in captured[1]['input']
    assert '정확성 또는 안전규칙 점수가 4 미만이면' in captured[1]['input']
    assert '현재 단계는 텍스트 심사다' in captured[1]['input']
