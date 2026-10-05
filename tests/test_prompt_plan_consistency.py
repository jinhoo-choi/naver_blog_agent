"""Offline checks of the actual model inputs against the current operating plan."""
import tomllib
from datetime import date
from pathlib import Path

import pytest

from blogbot.config import load_settings
from blogbot.inputs import ContentRequest
from blogbot.llm import BlogLLM
from blogbot.pre_review import DraftCandidate

ROOT = Path(__file__).resolve().parents[1]


def assert_daily_prompt(prompt, config):
    assert config['operating_plan']['version'] in prompt
    assert 'daily_plan' in prompt and '하루 글 1편' in prompt
    assert 'editorial.deep' in prompt
    weekday_names = ['월', '화', '수', '목', '금', '토', '일']
    schedule = config['operating_plan']['weekdays']
    for category in set(schedule):
        days = [name for name, selected in zip(weekday_names, schedule) if selected == category]
        label = '·'.join(days) + ('요일' if len(days) == 1 else '')
        assert f"{label}은 {config['categories'][category]['display_name']}" in prompt
    for obsolete in ['평일 Cloud 운영', '평일 배분', '주말 심화', 'editorial.weekend',
                     '육아·운동·투자 각 1건']:
        assert obsolete not in prompt
    images = config['images']
    assert images['parenting_count'] == images['exercise_count']
    assert f"육아·운동 각 {images['parenting_count']}장" in prompt
    assert f"투자는 기본 {images['investment_count']}장" in prompt
    assert f"{images['investment_extended_min_chars']:,}자 이상" in prompt
    assert f"{images['investment_extended_count']}장" in prompt


@pytest.mark.parametrize('filename', ['writer.md', 'reviewer.md'])
def test_prompt_files_match_daily_plan_and_image_configuration(filename):
    config = tomllib.loads((ROOT / 'config/blog.toml').read_text())
    assert_daily_prompt((ROOT / 'prompts' / filename).read_text(), config)


@pytest.mark.parametrize('day,category,subtype', [
    ('2026-10-07', 'parenting', 'article'),
    ('2026-10-08', 'exercise', 'article'),
    ('2026-10-09', 'investment', 'article'),
    ('2026-10-10', 'parenting', 'article'),
    ('2026-10-14', 'parenting', 'ai_tutorial'),
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
        provenance={'daily_plan': plan, 'editorial_type': subtype})
    info = settings.config['categories'][category]
    post = client.create_draft(request, info, [])
    review = client.review(post, info, request)
    client.rewrite(post, info, review, request)
    client.correct_draft(DraftCandidate(dict(payload), [source]), info, [], request)
    assert [c['stage'] for c in captured] == [
        'writer', 'reviewer', 'rewrite', 'pre_review_correction']
    for call in captured:
        assert_daily_prompt(call['input'], settings.config)
        assert '선택된 당일 주제의 심화 콘텐츠1편' in call['input']
        assert '"daily_plan"' in call['input']
        assert f'"category": "{category}"' in call['input']
    assert all(c['max_tool_calls'] == 3 for c in captured[:3])
    assert 'tools' not in captured[-1] and captured[-1]['max_output_tokens'] == 12000
    assert '총점 30점 중 24점 이상 PASS' in captured[1]['input']
    assert '정확성 또는 안전규칙 점수가 4 미만이면' in captured[1]['input']
    assert '현재 단계는 텍스트 심사다' in captured[1]['input']
