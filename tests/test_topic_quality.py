"""Offline contract/wiring checks, not an LLM or factual-quality evaluation."""
import json
from pathlib import Path

import pytest

from blogbot.core import PostDraft
from blogbot.editorial import TOPICS, editorial_hints, quality_guidance
from blogbot.images import image_prompt, section_contexts
from blogbot.inputs import ContentRequest
from blogbot.llm import BlogLLM

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / 'tests/fixtures/topic_quality.json').read_text())['cases']


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['topic'])
def test_four_distinct_editorial_cases(case):
    guide = quality_guidance(case['topic'])
    assert all(term in guide for term in case['required_guidance'])
    assert case['before'] != case['after']
    assert case['evidence'] and case['expected_review']
    assert '\n\n## ' in case['after']
    assert '창작하지 않는다' in guide
    assert '고정 목차가 아니다' in guide
    for other, text in TOPICS.items():
        if other != case['topic']:
            assert text not in guide


def test_hints_do_not_silently_delete_repeated_safety_or_invent_verdict():
    text = '통증이나 저림이 나타나면 즉시 운동을 멈추고 안전 조건을 확인합니다.'
    body = text + '\n\n## 다음 단계\n\n' + text
    hints = editorial_hints(body)
    assert len(hints) == 1 and '필수 안전 조건인지 확인' in hints[0]
    assert body.count(text) == 2
    assert editorial_hints('정상 문장입니다.\n\n다음 문단입니다.') == []
    assert '단어 분리' in editorial_hints('단\n어가 갈라졌습니다.')[0]
    assert '인용이면 보존' in editorial_hints('“살펴보도록 하겠습니다”라는 실제 발언입니다.')[0]


@pytest.mark.parametrize('category', ['parenting', 'exercise', 'investment'])
def test_all_llm_stages_receive_selected_contract_without_extra_calls(category, monkeypatch):
    from blogbot import llm

    captured = []
    source = 'https://example.org/fixture'
    response = {'output': [{'status': 'completed', 'action': {
        'type': 'open_page', 'url': source}}]}
    payload = {'title': '제목', 'subcategory': 'fixture', 'body': '답변입니다.\n\n## 조건\n\n설명입니다.',
               'tags': ['설명'], 'source_urls': [source], 'scores': [4] * 6, 'total': 24,
               'decision': 'PASS', 'issues': [], 'blocking_issues': [],
               'rewrite_instructions': '', 'source_checks': [
                   {'claim': 'fixture', 'source_url': source, 'evidence': 'fixture',
                    'status': 'SUPPORTED'}]}

    def fake_request(*args, **kwargs):
        captured.append(kwargs)
        return dict(payload), response

    monkeypatch.setattr(llm, 'request_json', fake_request)
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda *args: args[1])
    client = BlogLLM.__new__(BlogLLM)
    client.client, client.journal = None, None
    client.model = client.review_model = 'offline-test'
    client.writer_prompt = (ROOT / 'prompts/writer.md').read_text()
    client.reviewer_prompt = (ROOT / 'prompts/reviewer.md').read_text()
    request = ContentRequest('offline', category, {'question': 'fixture'})
    info = {'display_name': category, 'subcategories': ['fixture']}
    post = client.create_draft(request, info, [])
    review = client.review(post, info, request)
    client.rewrite(post, info, review, request)
    assert [c['stage'] for c in captured] == ['writer', 'reviewer', 'rewrite']
    for call in captured:
        assert quality_guidance(category) in call['input']
        assert call['max_tool_calls'] == 3
    assert review['scores'] == [4] * 6 and review['total'] == 24
    assert '점수를 올리지 않는다' in captured[1]['input']


def test_image_context_contains_explanation_and_retains_style_and_safety():
    body = '도입입니다.\n\n## 자세\n\n손의 접점과 몸통 위치를 확인합니다.\n\n## 중단\n\n통증이면 멈춥니다.'
    contexts = section_contexts(body)
    assert len(contexts) == 2 and '손의 접점' in contexts[0] and '통증이면' in contexts[1]
    post = PostDraft('exercise', '운동', 'topic', '동작 설명', body, [], [], '2026-10-03')
    prompt = image_prompt(post, contexts[0])
    for term in ['손의 접점', 'source-supported posture', 'guessed demonstration',
                 'never evidence of real use', 'sage-green', 'warm-yellow',
                 'No labels', 'unsupported exercise technique']:
        assert term in prompt
    assert '통증이면' not in prompt  # Unrelated section is not duplicated into each prompt.
    assert len(image_prompt(post, 'x' * 5000)) < 4500  # Bounded section context.


@pytest.mark.parametrize('state', ['READY', 'STARTED', 'UNCERTAIN'])
def test_legacy_image_jobs_hold_without_calls_or_file_changes(tmp_path, monkeypatch, state):
    from types import SimpleNamespace

    from blogbot.images import ImagePending, generate_images

    folder = tmp_path / 'generated-images' / 'old-job'
    folder.mkdir(parents=True)
    old = folder / 'old-key.json'
    old.write_text(json.dumps({'state': state, 'attempts': 1, 'updated_at': 1234}))
    (folder / 'old-key.jpg').write_bytes(b'old-image')
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    monkeypatch.setattr('blogbot.images.OpenAI', lambda **kw: pytest.fail('No client/call allowed'))
    settings = SimpleNamespace(artifact_dir=tmp_path, config={'images': {'exercise_count': 2}})
    post = PostDraft('exercise', '운동', 'topic', '제목', '## 자세\n\n설명입니다.', [], [],
                     '2026-10-03', request_id='old-job')
    with pytest.raises(ImagePending, match='Legacy image checkpoints need reconciliation'):
        generate_images(settings, ContentRequest('old-job', 'exercise', {}), post)
    assert before == {p.name: p.read_bytes() for p in folder.iterdir()}


def test_cache_only_rewrite_tries_exact_legacy_input_without_paid_retry(monkeypatch):
    from blogbot import llm
    from blogbot.responses import ResponseFailure

    calls = []
    def missing(*args, **kwargs):
        calls.append(kwargs)
        raise ResponseFailure('rewrite', 'cached_response_unavailable')
    monkeypatch.setattr(llm, 'request_json', missing)
    client = BlogLLM.__new__(BlogLLM)
    client.client, client.journal, client.model = None, None, 'offline'
    client.writer_prompt = 'unchanged writer prompt\n'
    post = PostDraft('exercise', '운동', 'topic', '제목', '본문', [], [], '2026-10-03')
    request = ContentRequest('test', 'exercise', {})
    with pytest.raises(ResponseFailure, match='cached_response_unavailable'):
        client.rewrite(post, {}, {}, request, cache_only=True)
    assert len(calls) == 2 and all(c['cache_only'] is True for c in calls)
    assert quality_guidance('exercise') in calls[0]['input']
    assert 'topic-quality-v1' not in calls[1]['input']
    assert calls[1]['input'].startswith('unchanged writer prompt\n\n\n오늘')
    assert {k: v for k, v in calls[0].items() if k != 'input'} == {
        k: v for k, v in calls[1].items() if k != 'input'}


def test_new_image_plan_reuses_cache_and_holds_changed_input(tmp_path, monkeypatch):
    import base64
    from dataclasses import replace
    from types import SimpleNamespace

    from blogbot.images import ImagePending, generate_images

    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b'image').decode())])
    monkeypatch.setattr('blogbot.images.OpenAI', lambda **kw: SimpleNamespace(
        images=SimpleNamespace(generate=generate)))
    settings = SimpleNamespace(artifact_dir=tmp_path, openai_api_key='offline', config={
        'images': {'exercise_count': 2, 'concurrency': 1}})
    post = PostDraft('exercise', '운동', 'topic', '제목', '도입입니다.\n\n## 자세\n\n손 접점입니다.',
                     [], [], '2026-10-03', request_id='new-job')
    request = ContentRequest('new-job', 'exercise', {})
    first = generate_images(settings, request, post)
    assert len(calls) == 2 and len(first.photos) == 2
    assert all(p['policy_version'] == 'category-scene-v2' for p in first.photos)
    second = generate_images(settings, request, post)
    assert len(calls) == 2 and first.photos == second.photos
    folder = tmp_path / 'generated-images' / 'new-job'
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    with pytest.raises(ImagePending, match='Image plan changed'):
        generate_images(settings, request, replace(post, body=post.body + ' 설명이 바뀌었습니다.'))
    assert len(calls) == 2
    assert before == {p.name: p.read_bytes() for p in folder.iterdir()}


def test_short_answer_generates_only_distinct_section_images_and_preserves_budget(tmp_path, monkeypatch):
    import base64
    from types import SimpleNamespace

    from blogbot.images import ImagePending, generate_images

    calls = []
    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b'image').decode())])
    monkeypatch.setattr('blogbot.images.OpenAI', lambda **kw: SimpleNamespace(
        images=SimpleNamespace(generate=generate)))
    settings = SimpleNamespace(artifact_dir=tmp_path, openai_api_key='offline', config={
        'images': {'exercise_count': 5, 'concurrency': 1}})
    post = PostDraft('exercise', '운동', 'topic', '제목',
                     '도입입니다.\n\n## 자세\n손 접점을 확인합니다.\n\n'
                     '## 같은 설명\n손 접점을 확인합니다.\n\n## 빈 구역\n### 하위 제목',
                     [], [], '2026-10-10', request_id='short-images')
    request = ContentRequest('short-images', 'exercise', {})
    first = generate_images(settings, request, post)
    assert len(calls) == 2 and [p['role'] for p in first.photos] == ['thumbnail', 'section']
    assert first.photos[1]['section_heading'] == '자세'
    assert generate_images(settings, request, post).photos == first.photos and len(calls) == 2
    folder = tmp_path / 'generated-images' / post.request_id
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    # A later change in the ceiling cannot start a new paid plan or reset attempts.
    settings.config['images']['exercise_count'] = 1
    with pytest.raises(ImagePending, match='Image plan changed'):
        generate_images(settings, request, post)
    assert len(calls) == 2 and before == {p.name: p.read_bytes() for p in folder.iterdir()}


def test_selected_image_stays_with_its_section_after_duplicate_sections_are_skipped():
    from blogbot.presentation import render_segments

    post = PostDraft('exercise', '운동', 'topic', '제목',
                     '핵심 답변입니다.\n\n## 준비\n준비 설명입니다.\n\n## 준비 반복\n준비 설명입니다.'
                     '\n\n## 자세\n자세 설명입니다.\n\n## 주의\n중단 신호입니다.',
                     [], [], '2026-10-10', photos=[
                         {'file': 'cover.jpg', 'role': 'thumbnail'},
                         {'file': 'posture.jpg', 'role': 'section', 'section_heading': '자세'}])
    segments = render_segments(post)
    index = next(i for i, segment in enumerate(segments) if '자세 설명입니다.' in segment.html)
    assert segments[index + 1].photo['file'] == 'posture.jpg'
    post.body += '\n\n## 자세\n다른 설명입니다.'
    with pytest.raises(ValueError, match='Image section heading needs reconciliation'):
        render_segments(post)
