"""Offline transport guards for separately approved parenting repair."""
import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from blogbot import cloud, manual_repair, manual_requests
from blogbot.config import load_settings
from blogbot.core import connect_db


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / 'state'
    monkeypatch.setenv('BLOG_DATA_DIR', str(root))
    monkeypatch.setenv('OPENAI_API_KEY', 'offline')
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    settings = load_settings()
    with closing(connect_db(settings.db_path)):
        pass
    folder = root / 'manual-repairs' / 'owner-parenting-repair'
    folder.mkdir(parents=True)
    (folder / 'claim.json').write_text('{"status":"RESERVED"}')
    (folder / 'approved-packet.json').write_text('{}')
    return settings, root, folder, key


def test_encrypted_roundtrip_preserves_claim_and_rebases_photos(state, tmp_path):
    settings, root, folder, key = state
    image = folder / 'images' / ('a' * 64 + '.jpg')
    image.parent.mkdir()
    image.write_bytes(b'offline test image')
    (image.parent / '.image-plan').write_text('{}')
    result = {'post': {'photos': [{'file': str(image),
        'sha256': hashlib.sha256(image.read_bytes()).hexdigest()}]}, 'input': {'photos': []}}
    pin = 'c' * 64
    (folder / 'claim.json').write_text(json.dumps({'status': manual_repair.READY,
        'packet_sha256': pin, 'result_sha256': manual_requests._result_digest(result)}))
    (folder / 'result.json').write_text(json.dumps(result))
    (folder / 'paid-calls.json').write_text('[]')
    (folder / 'review-calls.json').write_text('[]')
    (folder / 'usage.jsonl').write_text('{}\n')
    for name in ['response-cache', 'source-evidence']:
        target = folder / name
        target.mkdir()
        filename = '2026-10-11-a.json' if name == 'response-cache' else 'reference-' + 'b'*64 + '.json'
        (target / filename).write_text('{}')
    output = tmp_path / 'handoff' / 'bundle.enc'
    cloud.pack(settings, output)
    restored = tmp_path / 'restored'
    cloud.extract_bundle(output.read_bytes(), restored, key)
    nested = restored / folder.relative_to(root)
    assert (nested / 'claim.json').read_bytes() == (folder / 'claim.json').read_bytes()
    photo = Path(json.loads((nested / 'result.json').read_text())['post']['photos'][0]['file'])
    assert photo.is_relative_to(restored)
    assert photo.read_bytes() == b'offline test image'
    assert json.loads((restored / 'ready.json').read_text())['posts'] == []
    assert manual_repair._cached(nested, pin)['cached'] is True
    photo.write_bytes(b'tampered')
    with pytest.raises(ValueError, match='Photo changed'):
        manual_repair._cached(nested, pin)


def test_stale_restore_fails_before_claim_loss(state, tmp_path):
    settings, root, folder, key = state
    output = tmp_path / 'handoff' / 'bundle.enc'
    cloud.pack(settings, output)
    (folder / 'paid-calls.json').write_text('[{"status":"STARTED"}]')
    with pytest.raises(ValueError, match='checkpoint conflict'):
        cloud.extract_bundle(output.read_bytes(), root, key)
    assert (folder / 'paid-calls.json').read_text() == '[{"status":"STARTED"}]'


@pytest.mark.parametrize('bad', ['secret.txt', 'extra/nested/file.json', 'images/not-a-hash.jpg'])
def test_pack_rejects_unmanaged_paths(state, tmp_path, bad):
    settings, _root, folder, _key = state
    target = folder / bad
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('unexpected')
    with pytest.raises(ValueError, match='manual repair'):
        cloud.pack(settings, tmp_path / 'handoff' / 'bundle.enc')


def test_separate_remote_history_namespace():
    packet = {'request_id': 'original-id', 'approval_reference': 'Sentinel_approval', 'question': 'q'}
    name = manual_requests.reservation_name('123', packet, 'repair')
    assert name.startswith('blog-state-123-repair-')
    assert name != manual_requests.reservation_name('123', packet, 'review')


def test_workflow_retains_owner_and_reservation_gates():
    workflow = (Path(__file__).parents[1] / '.github/workflows/blog-prepare.yml').read_text()
    assert 'manual-repair-reserve' in workflow
    assert "inputs.mode != 'manual-repair'" in workflow
    assert 'github.actor == github.repository_owner' in workflow
    assert 'group: blog-api-prepare' in workflow
    assert 'BLOG_MANUAL_REPAIR_PACKET' in workflow
