"""API-only preparation and authenticated encrypted handoff to the Work saver."""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sqlite3
import zipfile
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.fernet import Fernet

from .config import load_settings
from .core import connect_db, today_kst
from .images import atomic_json
from .inputs import enqueue_file
from .pipeline import run_daily
from .presentation import render_segments


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            redirected.remove_header('Authorization')
        return redirected


def github_get(path: str) -> bytes:
    repo = os.environ['GITHUB_REPOSITORY']
    request = Request('https://api.github.com/repos/' + repo + path,
                      headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
                               'Accept': 'application/vnd.github+json', 'User-Agent': 'blog-api-preparer'})
    with build_opener(SafeRedirect()).open(request, timeout=40) as response:
        data = response.read(60_000_001)
    if len(data) > 60_000_000:
        raise ValueError('Artifact size limit exceeded')
    return data


def extract_bundle(encrypted: bytes, directory: Path, key: str) -> None:
    data = Fernet(key.encode()).decrypt(encrypted)
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            target = (directory / info.filename).resolve()
            if not target.is_relative_to(directory.resolve()) or info.file_size > 25_000_000:
                raise ValueError('Invalid bundle member')
        archive.extractall(directory)
    metadata = json.loads((directory / 'bundle-info.json').read_text())
    old_root = Path(metadata['data_root'])
    def rebase(photos):
        for photo in photos:
            relative = Path(photo['file']).relative_to(old_root)
            photo['file'] = str((directory / relative).resolve())
    for path in (directory / 'drafts').glob('*.json'):
        payload = json.loads(path.read_text())
        rebase(payload['post']['photos'])
        rebase(payload['input']['photos'])
        atomic_json(path, payload)
    ready_path = directory / 'ready.json'
    if ready_path.exists():
        ready = json.loads(ready_path.read_text())
        for item in ready.get('posts', []):
            rebase(item['post']['photos'])
            for segment in item['segments']:
                if segment.get('photo'):
                    rebase([segment['photo']])
        atomic_json(ready_path, ready)
    with closing(connect_db(directory / 'blog.db')) as conn:
        with conn:
            for row in conn.execute('SELECT id,photos_json FROM posts').fetchall():
                photos = json.loads(row['photos_json']); rebase(photos)
                conn.execute('UPDATE posts SET photos_json=? WHERE id=?',
                             (json.dumps(photos, ensure_ascii=False), row['id']))


def restore(directory: Path) -> None:
    listing = json.loads(github_get('/actions/artifacts?per_page=100'))
    artifacts = [a for a in listing['artifacts']
                 if a['name'].startswith('blog-state-') and not a['expired']]
    if not artifacts:
        raise RuntimeError('No durable state. Explicit bootstrap required; refuse a fresh duplicate run.')
    latest = max(artifacts, key=lambda a: a['id'])
    archive = github_get(f"/actions/artifacts/{latest['id']}/zip")
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        encrypted = z.read('bundle.enc')
    extract_bundle(encrypted, directory, os.environ['BLOG_BUNDLE_KEY'])


def seed_inputs(settings) -> None:
    seed = json.loads(os.environ.get('BLOG_SEED_JSON', '{}'))
    for item in seed.get('requests', []):
        if not (settings.inbox_dir / item['id']).exists():
            temporary = settings.db_path.parent / 'seed-request.json'
            atomic_json(temporary, item)
            enqueue_file(settings, temporary)
            temporary.unlink()
    with closing(connect_db(settings.db_path)) as conn:
        # Persist already published source identities; never generate them again.
        with conn:
            for request_id in seed.get('consumed_request_ids', []):
                conn.execute("INSERT OR IGNORE INTO attempts(day,category,request_id,status) "
                             "VALUES(?,?,?,?)", ('2026-09-27', 'investment', request_id, 'SAVED_NAVER'))
    atomic_json(settings.db_path.parent / 'context.json',
                {k: seed.get(k, [] if k != 'exclude_investment_topics_until' else '') for k in ['published_titles', 'exclude_investment_topics_until', 'excluded_investment_topics']})


def pack(settings, destination: Path) -> None:
    directory = settings.db_path.parent
    today = today_kst()
    cutoff = (today-timedelta(days=7)).isoformat()
    packet = []
    # Render transport segments; the saver can paste them without another generation.
    from .core import load_post
    with closing(connect_db(settings.db_path)) as conn:
        rows = conn.execute("SELECT * FROM posts WHERE status='APPROVED' AND as_of_date=?",
                            (today.isoformat(),)).fetchall()
        for row in rows:
            post = load_post(row)
            packet.append({'id': row['id'], 'category_no': settings.config['categories'][post.category]['naver_category_no'],
                           'post': post.__dict__, 'segments': [s.__dict__ for s in render_segments(post)]})
    atomic_json(directory / 'ready.json', {'date': today.isoformat(), 'posts': packet})
    atomic_json(directory / 'bundle-info.json', {'data_root': str(directory.resolve()),
                                                'date': today.isoformat(), 'version': 1})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        # Retain state and recent packets; old completed media are not transported forever.
        keep_ids = set()
        for path in settings.artifact_dir.glob('*.json'):
            payload = json.loads(path.read_text())
            if payload['post']['as_of_date'] >= cutoff:
                keep_ids.add(payload['post']['request_id'])
                archive.write(path, path.relative_to(directory))
                md = path.with_suffix('.md')
                if md.exists(): archive.write(md, md.relative_to(directory))
        for request_id in keep_ids:
            for path in (settings.artifact_dir/'generated-images'/request_id).glob('*'):
                if path.is_file() and path.suffix != '.tmp':
                    archive.write(path, path.relative_to(directory))
        for path in settings.inbox_dir.glob('*/request.json'):
            archive.write(path, path.relative_to(directory))
        for name in ['blog.db', 'bundle-info.json', 'ready.json', 'context.json',
                     'topic-cache.json', 'topic-selection.json']:
            path = directory/name
            if path.exists(): archive.write(path, name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(Fernet(os.environ['BLOG_BUNDLE_KEY'].encode()).encrypt(buffer.getvalue()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['bootstrap', 'prepare', 'unpack'])
    parser.add_argument('--file', type=Path)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if args.mode == 'unpack':
        extract_bundle(args.file.read_bytes(), args.destination, os.environ['BLOG_BUNDLE_KEY'])
        # Recreate packet segments with local image paths after relocation.
        print(json.dumps({'status': 'UNPACKED', 'directory': str(args.destination)}))
        return
    settings = load_settings()
    directory = settings.db_path.parent
    destination = Path(os.environ.get('BLOG_ENCRYPTED_OUTPUT', 'handoff/bundle.enc'))
    if args.mode == 'bootstrap':
        listing = json.loads(github_get('/actions/artifacts?per_page=100'))
        if any(a['name'].startswith('blog-state-') and not a['expired'] for a in listing['artifacts']):
            restore(directory)
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key, timeout=20, max_retries=0)
        for model in {settings.openai_model, settings.review_model, settings.config['images']['model']}:
            client.models.retrieve(model)
        directory.mkdir(parents=True, exist_ok=True)
        seed_inputs(settings)
        pack(settings, destination)
        print(json.dumps({'status': 'BOOTSTRAPPED', 'generation_calls': 0}))
        return
    restore(directory)
    try:
        seed_inputs(settings)
        import holidays
        today = today_kst()
        if today.weekday() >= 5 or today in holidays.KR(years=today.year) or (today.month, today.day)==(5,1):
            print(json.dumps({'status': 'NON_BUSINESS_DAY'}))
            return
        results = run_daily(settings, count=3, save_to_naver=False)
        print(json.dumps(results, ensure_ascii=False))
    finally:
        # Checkpoints survive handled API errors; no raw files are uploaded to the public repository.
        pack(settings, destination)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'ERROR', 'error': type(exc).__name__}))
        raise SystemExit(1) from None
