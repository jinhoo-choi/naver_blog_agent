"""Offline review-style routing, media and save boundaries; no Naver/paid requests."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from blogbot.config import load_settings
from blogbot.core import PostDraft
from blogbot.editorial import REVIEW, content_style, quality_guidance, routed_prompt
from blogbot.images import ImagePending, generate_images
from blogbot.inputs import ContentRequest, collect_requests, enqueue_file
from blogbot.llm import BlogLLM
from blogbot.naver import NaverDraftWriter
from blogbot.planning import matches_request, resolve_plan
from blogbot.pre_review import DraftCandidate, inspect_candidate
from blogbot.presentation import render_segments

ROOT = Path(__file__).resolve().parents[1]


def review_post():
    return PostDraft('parenting', '육아', 'topic', '사용 후기',
                     '제공된 경험입니다.\n\n## 착용\n\n제공된 착용 경험입니다.\n\n'
                     '## 포장\n\n포장 설명입니다.\n\n## 구성\n\n판매자 구성 안내입니다.',
                     ['사용후기'], [], '2026-10-06', request_id='review')


def photo(tmp_path, name, **metadata):
    path = tmp_path / (name + '.png')
    path.write_bytes(name.encode())
    return {'file': str(path), 'caption': name,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), **metadata}


@pytest.mark.parametrize('category', ['parenting', 'exercise', 'cooking'])
def test_explicit_review_only_and_plan_unchanged(category):
    assert REVIEW in quality_guidance(category, style='review')
    assert REVIEW not in quality_guidance(category)
    assert content_style(category, {'question': '제품 후기 의료 논문 review'}) == 'article'
    request = ContentRequest('review', category, {'content_style': 'review'})
    plan = {'category': category, 'editorial_types': ['article']}
    assert matches_request(request, plan)  # style does not consume a different slot


@pytest.mark.parametrize('category,data', [
    ('investment', {'content_style': 'review'}),
    ('parenting', {'content_style': 'review', 'editorial_type': 'ai_tutorial'}),
    ('exercise', {'content_style': 'unknown'}),
    ('parenting', {'content_style': []}),
])
def test_unsupported_styles_fail_closed(category, data):
    with pytest.raises(ValueError, match='Unsupported content style'):
        content_style(category, data)


def test_default_prompt_is_byte_identical_and_review_keeps_safety():
    for filename in ['writer', 'reviewer']:
        prompt = (ROOT / f'prompts/{filename}.md').read_text()
        assert routed_prompt(prompt, 'parenting') == prompt
        selected = routed_prompt(prompt, 'parenting', style='review')
        assert '생성 이미지 기준은 육아' not in selected
        assert '관련 없는 월령표를 강제하지 않는다' in selected or filename == 'reviewer'
        assert '안전' in selected and '출처' in selected
    for term in ['자비 구매', '리뷰 대가 없음', '선택 가능한 적격', '개인정보', '배송 라벨',
                 '가격 생략', '설치 시간', '가족 반응', '단점', '1~3문장', '한 가지',
                 '2열', '최대 8개', '자동 재열기 검증을 추가하지 않는다']:
        assert term in REVIEW


def test_all_four_model_stages_use_review_contract_without_extra_calls(monkeypatch):
    from blogbot import llm

    calls = []
    source = 'https://example.org/product'
    payload = {'title': '제목', 'subcategory': '육아', 'body': '실제 제공 경험입니다.',
               'tags': ['제품후기'], 'source_urls': [source], 'scores': [4] * 6,
               'total': 24, 'decision': 'PASS', 'issues': [], 'blocking_issues': [],
               'rewrite_instructions': '', 'source_checks': [{
                   'claim': 'fixture', 'source_url': source, 'evidence': 'fixture',
                   'status': 'SUPPORTED'}]}
    response = {'output': [{'status': 'completed', 'action': {'type': 'open_page', 'url': source}}]}

    def fake(*args, **kwargs):
        calls.append(kwargs)
        return dict(payload), response

    monkeypatch.setattr(llm, 'request_json', fake)
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda *args: args[1])
    client = BlogLLM.__new__(BlogLLM)
    client.client, client.journal = None, None
    client.model = client.review_model = 'offline'
    client.writer_prompt = (ROOT / 'prompts/writer.md').read_text()
    client.reviewer_prompt = (ROOT / 'prompts/reviewer.md').read_text()
    request = ContentRequest('review', 'parenting', {'question': '제품 후기',
                                                  'content_style': 'review'})
    info = {'display_name': '육아', 'subcategories': ['육아']}
    draft = client.create_draft(request, info, [])
    client.correct_draft(DraftCandidate(payload, [source]), info, ['fixture'], request)
    review = client.review(draft, info, request)
    client.rewrite(draft, info, review, request)
    assert [c['stage'] for c in calls] == ['writer', 'pre_review_correction', 'reviewer', 'rewrite']
    for call in calls:
        assert REVIEW in call['input']
        assert '생성 이미지 기준은 육아' not in call['input']
    assert 'tools' not in calls[1]
    assert all(c['max_tool_calls'] == 3 for c in (calls[0], calls[2], calls[3]))
    assert review['scores'] == [4] * 6


def test_private_enqueue_retains_review_metadata_and_photo_hashes(tmp_path, monkeypatch, weekday_clock):
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path / 'data'))
    settings = load_settings()
    settings.config['topics'].pop('scheduled', None)
    settings.config['community']['enabled'] = False
    image = photo(tmp_path, 'owner', role='hero')
    raw = {'id': 'owner-review', 'category': 'parenting', 'question': '제품 후기',
           'content_style': 'review', 'context': '가격은 생략합니다.', 'photos': [image]}
    source = tmp_path / 'input.json'
    source.write_text(json.dumps(raw))
    enqueue_file(settings, source)
    requests, notices = collect_requests(settings)
    assert not notices and len(requests) == 1
    request = requests[0]
    assert request.data['content_style'] == 'review'
    assert request.data['context'] == raw['context']
    assert request.photos[0]['role'] == 'hero'
    prompt = json.dumps(request.prompt_data())
    assert str(tmp_path) not in prompt and 'sha256' not in prompt
    Path(request.photos[0]['file']).write_bytes(b'changed')
    assert collect_requests(settings) == ([], [{'status': 'INPUT_REJECTED'}])


def test_review_uses_real_photos_no_paid_call_and_preserves_owner_before_seller(tmp_path, monkeypatch):
    monkeypatch.setattr('blogbot.images.OpenAI', lambda **kw: pytest.fail('No paid image client'))
    settings = SimpleNamespace(artifact_dir=tmp_path, config={})
    photos = [photo(tmp_path, 'seller', origin='seller', source_url='https://example.org/product'),
              photo(tmp_path, 'detail', section_heading='포장'),
              photo(tmp_path, 'hero', role='hero')]
    request = ContentRequest('review', 'parenting', {'content_style': 'review'}, photos)
    post = generate_images(settings, request, review_post())
    assert [p['caption'] for p in post.photos] == ['hero', 'detail', 'seller']
    segments = render_segments(post)
    assert [s.photo['caption'] for s in segments if s.photo] == ['hero', 'detail', 'seller']
    assert segments[1].photo['caption'] == 'hero'
    detail = next(i for i, s in enumerate(segments) if s.photo and s.photo['caption'] == 'detail')
    assert '포장 설명입니다.' in segments[detail - 1].html
    assert '판매자 제공 구성 자료' in segments[-1].html
    assert '#사용후기' in segments[-1].html
    assert '#사용후기' not in render_segments(post, include_tags=False)[-1].html
    # No model invocation or new image-plan directory for real supplied evidence.
    assert not (tmp_path / 'generated-images').exists()


@pytest.mark.parametrize('case', ['missing', 'only_seller', 'unmatched_heading', 'old_checkpoint'])
def test_review_media_holds_without_generating_or_resetting_history(tmp_path, monkeypatch, case):
    monkeypatch.setattr('blogbot.images.OpenAI', lambda **kw: pytest.fail('No paid image client'))
    settings = SimpleNamespace(artifact_dir=tmp_path, config={})
    photos = [photo(tmp_path, 'owner')]
    if case == 'missing':
        photos = []
    elif case == 'only_seller':
        photos = [photo(tmp_path, 'seller', origin='seller', source_url='https://example.org/product')]
    elif case == 'unmatched_heading':
        photos[0]['section_heading'] = '없는 제목'
    else:
        folder = tmp_path / 'generated-images' / 'review'
        folder.mkdir(parents=True)
        (folder / 'state.json').write_text('{"state":"UNCERTAIN","attempts":1}')
    request = ContentRequest('review', 'parenting', {'content_style': 'review'}, photos)
    with pytest.raises(ImagePending):
        generate_images(settings, request, review_post())
    if case == 'old_checkpoint':
        assert (folder / 'state.json').read_text() == '{"state":"UNCERTAIN","attempts":1}'


def test_seller_cannot_precede_mapped_owner_evidence(tmp_path):
    post = replace(review_post(), provenance={'content_style': 'review'}, photos=[
        photo(tmp_path, 'owner', section_heading='포장'),
        photo(tmp_path, 'seller', origin='seller', section_heading='착용',
              source_url='https://example.org/product')])
    with pytest.raises(ValueError, match='must follow owner'):
        render_segments(post)


def test_unverified_direct_writer_cannot_upload_review_photos(tmp_path):
    writer = NaverDraftWriter('owner', str(tmp_path), categories={'parenting': {'naver_category_no': 1}})
    with pytest.raises(RuntimeError, match='Work visual privacy/layout checks'):
        writer.preflight(replace(review_post(), provenance={'content_style': 'review'}))


def test_review_missing_photos_is_pre_review_hard_stop():
    payload = {'title': '제목', 'subcategory': '육아', 'topic': '후기', 'body': '후기 본문',
               'tags': ['후기'], 'source_urls': ['https://example.org/product'],
               'as_of_date': '2026-10-06'}
    request = ContentRequest('review', 'parenting', {'content_style': 'review'})
    _, codes, _ = inspect_candidate(DraftCandidate(payload, payload['source_urls']), request, {}, False)
    assert 'missing_owner_photos' in codes


def test_plan_reservations_and_subtypes_are_unchanged():
    from datetime import date
    config = {'operating_plan': {'enabled': True, 'start_date': '2026-10-05',
              'version': 'daily-deep-511-v1', 'weekdays': ['parenting', 'parenting', 'exercise',
              'parenting', 'investment', 'parenting', 'parenting']}}
    assert resolve_plan(config, date(2026, 10, 6))['editorial_types'] == ['article', 'ai_tutorial']
    assert resolve_plan(config, date(2026, 10, 9))['category'] == 'investment'


def test_review_survives_database_and_encrypted_handoff(tmp_path, monkeypatch, weekday_clock):
    from contextlib import closing

    from cryptography.fernet import Fernet

    from blogbot.cloud import extract_bundle, pack
    from blogbot.core import connect_db, load_post, save_post, today_kst
    from blogbot.pipeline import complete_media, save_pending

    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('NAVER_BLOG_ID', 'owner')
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    monkeypatch.setattr('blogbot.images.OpenAI', lambda **kw: pytest.fail('No image call'))
    settings = load_settings()
    settings.config['topics'].pop('scheduled', None)
    settings.config['community']['enabled'] = False
    supplied = photo(tmp_path, 'supplied', role='hero')
    source = tmp_path / 'request.json'
    source.write_text(json.dumps({'id': 'review', 'category': 'parenting',
                                 'question': '실제 제품 사용 후기', 'content_style': 'review',
                                 'photos': [supplied]}))
    enqueue_file(settings, source)
    request = collect_requests(settings)[0][0]
    post = replace(review_post(), as_of_date=str(today_kst()), photos=request.photos,
                   provenance=request.provenance, status='TEXT_APPROVED')
    settings.artifact_dir.mkdir(parents=True)
    with closing(connect_db(settings.db_path)) as conn:
        post_id = save_post(conn, post)
        result = complete_media(settings, conn, post_id, post, request, {'scores': [4] * 6})
        assert result['status'] == 'APPROVED'
        stored = load_post(conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone())
        assert stored.provenance['content_style'] == 'review'
    assert save_pending(settings)[0]['status'] == 'SETUP_REQUIRED'
    destination = tmp_path / 'bundle.enc'
    pack(settings, destination)
    restored = tmp_path / 'restored'
    extract_bundle(destination.read_bytes(), restored, key)
    queue = json.loads((restored / 'inbox' / 'review' / 'request.json').read_text())
    assert Path(queue['photos'][0]['file']).read_bytes() == b'supplied'
    assert Path(queue['photos'][0]['file']).is_relative_to(restored)
    ready = json.loads((restored / 'ready.json').read_text())['posts'][0]
    assert ready['post']['provenance']['content_style'] == 'review'
    assert ready['segments'][1]['photo']['role'] == 'hero'
    assert Path(ready['segments'][1]['photo']['file']).read_bytes() == b'supplied'
    with closing(connect_db(restored / 'blog.db')) as conn:
        recovered = load_post(conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone())
        assert recovered.provenance['content_style'] == 'review'
        assert Path(recovered.photos[0]['file']).is_relative_to(restored)


def test_generated_photo_cannot_be_claimed_as_review_evidence(tmp_path):
    from blogbot.inputs import review_photo_metadata
    with pytest.raises(ValueError, match='supplied actual photo'):
        review_photo_metadata(photo(tmp_path, 'fake', generated=True))


def test_transport_budget_rejects_before_copy_and_rechecks_restored_queue(
    tmp_path, monkeypatch, weekday_clock,
):
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path / 'data'))
    settings = load_settings()
    settings.config['topics'].pop('scheduled', None)
    settings.config['community']['enabled'] = False
    source = tmp_path / 'input.json'
    supplied = photo(tmp_path, 'owner')
    source.write_text(json.dumps({'id': 'review', 'category': 'parenting', 'question': '실제 후기',
                                 'content_style': 'review', 'photos': [supplied]}))
    monkeypatch.setattr('blogbot.inputs.MAX_MANAGED_PHOTO_BYTES', 1)
    with pytest.raises(ValueError, match='transport capacity'):
        enqueue_file(settings, source)
    assert not (settings.inbox_dir / 'review').exists()
    monkeypatch.setattr('blogbot.inputs.MAX_MANAGED_PHOTO_BYTES', 100)
    enqueue_file(settings, source)
    assert len(collect_requests(settings)[0]) == 1
    before = (settings.inbox_dir / 'review' / 'photo-01.png').read_bytes()
    monkeypatch.setattr('blogbot.inputs.MAX_MANAGED_PHOTO_BYTES', 1)
    assert collect_requests(settings) == ([], [{'status': 'INPUT_REJECTED'}])
    assert (settings.inbox_dir / 'review' / 'photo-01.png').read_bytes() == before


def test_invalid_layout_stays_pending_and_pack_preserves_checkpoints(tmp_path, monkeypatch, weekday_clock):
    from contextlib import closing

    from cryptography.fernet import Fernet

    from blogbot.cloud import extract_bundle, pack
    from blogbot.core import connect_db, save_post, today_kst
    from blogbot.pipeline import complete_media

    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path / 'data'))
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    settings = load_settings()
    settings.config['topics'].pop('scheduled', None)
    settings.config['community']['enabled'] = False
    photos = [photo(tmp_path, 'owner', section_heading='포장'),
              photo(tmp_path, 'seller', origin='seller', section_heading='착용',
                    source_url='https://example.org/product')]
    source = tmp_path / 'request.json'
    source.write_text(json.dumps({'id': 'review', 'category': 'parenting', 'question': '실제 후기',
                                 'content_style': 'review', 'photos': photos}))
    enqueue_file(settings, source)
    request = collect_requests(settings)[0][0]
    post = replace(review_post(), as_of_date=str(today_kst()), photos=request.photos,
                   provenance=request.provenance, status='TEXT_APPROVED')
    settings.artifact_dir.mkdir(parents=True)
    with closing(connect_db(settings.db_path)) as conn:
        post_id = save_post(conn, post)
        assert complete_media(settings, conn, post_id, post, request, {})['status'] == 'IMAGES_PENDING'
    destination = tmp_path / 'bundle.enc'
    pack(settings, destination)
    restored = tmp_path / 'restored'
    extract_bundle(destination.read_bytes(), restored, key)
    assert json.loads((restored / 'ready.json').read_text())['posts'] == []
    with closing(connect_db(restored / 'blog.db')) as conn:
        assert conn.execute('SELECT status FROM posts WHERE id=?', (post_id,)).fetchone()[0] == 'IMAGES_PENDING'


def test_oversize_bundle_preserves_existing_destination(tmp_path, monkeypatch, weekday_clock):
    from cryptography.fernet import Fernet

    from blogbot.cloud import pack
    from blogbot.core import connect_db

    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setenv('BLOG_BUNDLE_KEY', Fernet.generate_key().decode())
    monkeypatch.setattr('blogbot.cloud.MAX_ENCRYPTED_BUNDLE_BYTES', 1)
    settings = load_settings()
    settings.artifact_dir.mkdir(parents=True)
    connect_db(settings.db_path).close()
    destination = tmp_path / 'bundle.enc'
    destination.write_bytes(b'prior verified checkpoint')
    with pytest.raises(ValueError, match='existing destination preserved'):
        pack(settings, destination)
    assert destination.read_bytes() == b'prior verified checkpoint'
