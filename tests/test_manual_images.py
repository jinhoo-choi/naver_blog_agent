import base64
import hashlib
import io
import json
import zipfile
from contextlib import closing
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet, InvalidToken

from blogbot import cloud, manual_images
from blogbot.config import load_settings
from blogbot.core import connect_db
from blogbot.images import atomic_json


@pytest.fixture
def manual(tmp_path, monkeypatch):
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    settings = load_settings()
    settings.artifact_dir.mkdir(parents=True)
    with closing(connect_db(settings.db_path)) as conn, conn:
        conn.execute("INSERT INTO save_receipts(request_id,day,category) VALUES(?,?,?)",
                     (manual_images.REQUEST_ID, '2026-10-10', 'investment'))
    packet = {
        'request_id': manual_images.REQUEST_ID, 'category': 'investment',
        'as_of_date': '2026-10-09', 'subcategory': 'test', 'title': 'test title',
        'body': 'Opening\n\n## Policy\nFirst\n## Company A\nSecond\n'
                '## Company B\nThird\n## Market\nFourth',
        'tags': [], 'source_urls': [], 'status': 'LOCAL_TEXT_REVIEWED',
        'manual_owner_replacement': True, 'published': False, 'image_ceiling': 3,
        'text_review': {'reviewer': 'independent', 'accuracy': 5, 'safety': 5},
    }
    raw = json.dumps(packet).encode()
    monkeypatch.setattr(manual_images, 'PACKET_SHA256', hashlib.sha256(raw).hexdigest())
    monkeypatch.setattr(manual_images, 'BODY_SHA256',
                        hashlib.sha256(packet['body'].encode()).hexdigest())
    key = Fernet.generate_key()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key.decode())
    token = Fernet(key).encrypt(raw).decode()
    calls = []

    def generate(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b'image').decode())])

    monkeypatch.setattr('blogbot.images.OpenAI',
                        lambda **kwargs: SimpleNamespace(images=SimpleNamespace(generate=generate)))
    return settings, packet, token, key, calls


def test_image_only_cache_preserves_ledger_and_manual_status(manual):
    settings, packet, token, _, calls = manual
    before = settings.db_path.read_bytes()
    for _ in range(2):
        result = manual_images.run_manual_images(settings, token, packet['request_id'])
        assert result['status'] == 'MANUAL_IMAGES_READY'
        assert result['requires_visual_review'] is True
    assert len(calls) == 3
    assert settings.db_path.read_bytes() == before
    assert any('Section: Company A' in call['prompt'] for call in calls)
    assert any('Section: Company B' in call['prompt'] for call in calls)
    output = json.loads(next(settings.artifact_dir.glob('manual-images-*.json')).read_text())
    assert output['approved_packet'] == packet
    assert output['post']['status'] == 'LOCAL_TEXT_REVIEWED'
    assert output['post']['body'] == packet['body']
    assert output['post']['as_of_date'] == '2026-10-09'


@pytest.mark.parametrize('invalid', ['id', 'plaintext', 'oversize', 'changed_packet', 'wrong_key'])
def test_invalid_input_never_calls_api(manual, invalid):
    settings, packet, token, key, calls = manual
    request_id = packet['request_id']
    if invalid == 'id':
        request_id = 'other'
    elif invalid == 'plaintext':
        token = json.dumps(packet)
    elif invalid == 'oversize':
        token = 'a' * 40_001
    elif invalid == 'changed_packet':
        token = Fernet(key).encrypt(json.dumps({**packet, 'title': 'changed'}).encode()).decode()
    else:
        token = Fernet(Fernet.generate_key()).encrypt(json.dumps(packet).encode()).decode()
    with pytest.raises((ValueError, InvalidToken)):
        manual_images.run_manual_images(settings, token, request_id)
    assert calls == []
    assert not list(settings.artifact_dir.iterdir())


@pytest.mark.parametrize('state', ['STARTED', 'UNCERTAIN', 'FAILED', 'READY', 'UNKNOWN', None])
def test_uncertain_never_resubmits_even_after_cooldown(manual, state):
    settings, packet, token, _, calls = manual
    manual_images.run_manual_images(settings, token, packet['request_id'])
    folder = settings.artifact_dir / 'generated-images' / packet['request_id']
    manifest = next(folder.glob('*.json'))
    data = json.loads(manifest.read_text())
    (folder / (manifest.stem + '.jpg')).unlink()
    atomic_json(manifest, {**data, 'state': state, 'updated_at': 1})
    result = manual_images.run_manual_images(settings, token, packet['request_id'])
    assert result['status'] == 'IMAGES_PENDING'
    assert len(calls) == 3
    assert json.loads(manifest.read_text())['attempts'] == 1


def test_encrypted_transport_retains_manual_checkpoints_and_not_ready(manual, tmp_path):
    settings, packet, token, key, _ = manual
    manual_images.run_manual_images(settings, token, packet['request_id'])
    destination = tmp_path / 'encrypted' / 'bundle.enc'
    cloud.pack(settings, destination)
    with zipfile.ZipFile(io.BytesIO(Fernet(key).decrypt(destination.read_bytes()))) as archive:
        names = archive.namelist()
        assert any('manual-images-' in name for name in names)
        assert any(name.endswith('.image-plan') for name in names)
        assert len([name for name in names if name.endswith('.jpg')]) == 3
        assert json.loads(archive.read('ready.json'))['posts'] == []
    restored = tmp_path / 'restored'
    cloud.extract_bundle(destination.read_bytes(), restored, key.decode())
    payload = json.loads(next((restored / 'drafts').glob('manual-images-*.json')).read_text())
    assert all(photo['file'].startswith(str(restored)) for photo in payload['post']['photos'])


def test_cloud_manual_skips_every_text_and_queue_stage(manual, monkeypatch, tmp_path):
    settings, packet, token, _, _ = manual
    monkeypatch.setattr(cloud, 'load_settings', lambda: settings)
    monkeypatch.setattr(cloud, 'restore', lambda directory: None)
    monkeypatch.setattr(cloud, 'seed_inputs', lambda _: pytest.fail('Input seeding'))
    monkeypatch.setattr(cloud, 'run_daily', lambda *a, **kw: pytest.fail('Daily execution'))
    monkeypatch.setattr(cloud, 'recover_preparation', lambda *a: pytest.fail('Recovery'))
    monkeypatch.setenv('BLOG_MANUAL_IMAGE_PACKET', token)
    monkeypatch.setenv('BLOG_ENCRYPTED_OUTPUT', str(tmp_path / 'handoff' / 'bundle.enc'))
    monkeypatch.setattr('sys.argv', ['cloud', 'manual-images', '--manual-image-request-id',
                                    packet['request_id']])
    cloud.main()
    assert (tmp_path / 'handoff' / 'bundle.enc').exists()
