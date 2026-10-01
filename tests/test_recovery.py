import base64
import json
from contextlib import closing
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from openai import APIStatusError

from blogbot.cloud import extract_bundle, filter_ready, pack
from blogbot.config import load_settings
from blogbot.core import PostDraft, connect_db, save_post, today_kst
from blogbot.images import ImagePending, atomic_json, generate_images
from blogbot.inputs import ContentRequest
from blogbot.pipeline import run_daily


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    result = load_settings()
    result.config['images']['parenting_count'] = 1
    result.artifact_dir.mkdir(parents=True)
    return result


def draft(age=0, status='APPROVED'):
    return PostDraft('parenting', '정보', '질문', f'제목 {age}', '## 확인\n본문', [], [],
                     (today_kst()-timedelta(days=age)).isoformat(), 27, status,
                     request_id=f'question-{age}')


@pytest.mark.parametrize('status', ['TEXT_APPROVED', 'IMAGES_PENDING'])
@pytest.mark.parametrize('age', [-1, 0, 1, 3, 4])
def test_resume_preserves_original_text_and_date(settings, monkeypatch, age, status):
    post = draft(age, status)
    request = ContentRequest(post.request_id, post.category, {'question': '원래 질문'})
    with closing(connect_db(settings.db_path)) as conn:
        post_id = save_post(conn, post)
    atomic_json(settings.artifact_dir / f'{post.as_of_date}-{post_id:05d}.json',
                {'post': asdict(post), 'input': asdict(request), 'review': {'total': 27}})
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: ([], []))
    monkeypatch.setattr('blogbot.pipeline.rank_candidates', lambda *args: [])
    calls = []

    def media(settings, request, actual):
        calls.append((request, actual))
        return actual

    monkeypatch.setattr('blogbot.pipeline.generate_images', media)
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', lambda *args: pytest.fail('New LLM call'))
    run_daily(settings)
    assert len(calls) == int(0 <= age <= 3)
    if calls:
        assert calls[0][0] == request
        assert calls[0][1].as_of_date == post.as_of_date
        assert calls[0][1].body == post.body
    with closing(connect_db(settings.db_path)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


def test_pack_and_work_receipts_across_dates(settings, monkeypatch, tmp_path):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    with closing(connect_db(settings.db_path)) as conn:
        for age in [-1, 0, 1, 2, 3, 4]:
            save_post(conn, draft(age))
        save_post(conn, replace(draft(0), title='이미 저장', status='SAVED_NAVER'))
    encrypted = tmp_path / 'bundle.enc'
    pack(settings, encrypted)
    directory = tmp_path / 'work'
    extract_bundle(encrypted.read_bytes(), directory, key)
    path = directory / 'ready.json'
    ready = json.loads(path.read_text())
    assert [p['post']['request_id'] for p in ready['posts']] == [
        'question-3', 'question-2', 'question-1', 'question-0',
    ]
    ready['posts'].append(ready['posts'][-1])
    atomic_json(path, ready)
    receipts = {'verified_date': today_kst().isoformat(), 'records': [
        {'request_id': f'question-{age}', 'status': status}
        for age, status in [(1, 'SAVED_NAVER'), (2, 'SAVE_UNCERTAIN'), (3, 'PUBLISHED')]
    ]}
    filter_ready(directory, receipts)
    assert [p['post']['request_id'] for p in json.loads(path.read_text())['posts']] == [
        'question-0',
    ]
    # The next delivery still carries API APPROVED; today's fresh Work ledger blocks it.
    receipts['records'].append({'request_id': 'question-0', 'status': 'SAVING'})
    extract_bundle(encrypted.read_bytes(), directory, key)
    filter_ready(directory, receipts)
    assert json.loads(path.read_text())['posts'] == []
    assert ready['posts'][0]['requires_fresh_review'] is True
    assert ready['posts'][-1]['requires_fresh_review'] is False
    for invalid in [{}, {**receipts, 'verified_date': '2000-01-01'},
                    {**receipts, 'records': [{'request_id': 'x', 'status': 'UNKNOWN'}]}]:
        with pytest.raises(ValueError):
            filter_ready(directory, invalid)


@pytest.mark.parametrize('state_name', ['STARTED', 'UNCERTAIN', 'FAILED', 'RATE_LIMITED'])
@pytest.mark.parametrize('outcome', ['success', 'timeout', '429', 'crash'])
def test_image_recovery_is_one_call_after_six_hours(settings, monkeypatch, state_name, outcome):
    clock = [100_000.0]
    monkeypatch.setattr('blogbot.images.time.time', lambda: clock[0])
    monkeypatch.setattr('blogbot.images.time.sleep', lambda _: pytest.fail('Extra retry'))
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1 or outcome == 'timeout':
            raise RuntimeError('Timeout after submission')
        if outcome == '429':
            response = httpx.Response(429, request=httpx.Request('POST', 'https://example.com'))
            raise APIStatusError('Rate limited', response=response, body={})
        if outcome == 'crash':
            raise SystemExit('Simulated process death')
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b'image').decode())])

    monkeypatch.setattr('blogbot.images.OpenAI',
                        lambda **kwargs: SimpleNamespace(images=SimpleNamespace(generate=generate)))
    post = draft()
    request = ContentRequest(post.request_id, post.category, {})
    with pytest.raises(ImagePending):
        generate_images(settings, request, post)
    manifest = next((settings.artifact_dir / 'generated-images' / post.request_id).glob('*.json'))
    state = json.loads(manifest.read_text())
    atomic_json(manifest, {**state, 'state': state_name, 'attempts': 2})
    clock[0] += 6 * 60 * 60 - 1
    with pytest.raises(ImagePending):
        generate_images(settings, request, post)
    assert len(calls) == 1
    clock[0] += 1
    if outcome == 'success':
        assert len(generate_images(settings, request, post).photos) == 1
    else:
        with pytest.raises(SystemExit if outcome == 'crash' else ImagePending):
            generate_images(settings, request, post)
    assert json.loads(manifest.read_text())['recovery_attempted'] is True
    clock[0] += 24 * 60 * 60
    if outcome == 'success':
        assert len(generate_images(settings, request, post).photos) == 1
    else:
        with pytest.raises(ImagePending):
            generate_images(settings, request, post)
    assert len(calls) == 2


def test_legacy_manifest_starts_cooldown_without_api_call(settings, monkeypatch):
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        raise RuntimeError('Uncertain')

    monkeypatch.setattr('blogbot.images.OpenAI',
                        lambda **kwargs: SimpleNamespace(images=SimpleNamespace(generate=generate)))
    monkeypatch.setattr('blogbot.images.time.time', lambda: 100_000.0)
    post = draft()
    request = ContentRequest(post.request_id, post.category, {})
    with pytest.raises(ImagePending):
        generate_images(settings, request, post)
    manifest = next((settings.artifact_dir / 'generated-images' / post.request_id).glob('*.json'))
    atomic_json(manifest, {'state': 'STARTED', 'attempts': 1})
    with pytest.raises(ImagePending):
        generate_images(settings, request, post)
    assert json.loads(manifest.read_text())['updated_at'] == 100_000.0
    assert len(calls) == 1



def test_editorial_correction_preserves_identity_and_needs_new_review(tmp_path, monkeypatch):
    import hashlib

    from blogbot.recovery import editorial_patch
    key = Fernet.generate_key()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key.decode())
    post = replace(draft(), source_urls=['https://official.example/source'])
    root = SimpleNamespace(root=tmp_path)
    target = tmp_path / 'editorial' / post.as_of_date
    target.mkdir(parents=True)
    path = target / (hashlib.sha256(post.request_id.encode()).hexdigest() + '.enc')
    assert editorial_patch(root, post) == post
    data = {'request_id': post.request_id, 'as_of_date': post.as_of_date,
            'body': '검증 지적을 반영한 원고', 'source_urls': post.source_urls}
    path.write_bytes(Fernet(key).encrypt(json.dumps(data).encode()))
    corrected = editorial_patch(root, post)
    assert corrected.body != post.body
    assert corrected.request_id == post.request_id
    assert corrected.status == post.status  # Applying a patch does not change approval status.
    assert corrected.provenance == post.provenance
    for change in [{'request_id': 'other'}, {'as_of_date': '2000-01-01'},
                   {'source_urls': ['https://unobserved.example/']}, {'status': 'APPROVED'},
                   {'body': ''}]:
        path.write_bytes(Fernet(key).encrypt(json.dumps({**data, **change}).encode()))
        with pytest.raises(ValueError):
            editorial_patch(root, post)

    path.write_bytes(Fernet(Fernet.generate_key()).encrypt(json.dumps(data).encode()))
    with pytest.raises(ValueError, match='decryption'):
        editorial_patch(root, post)
