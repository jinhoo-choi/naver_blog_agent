"""API-only preparation and authenticated encrypted handoff to the Work saver."""
from __future__ import annotations

import argparse
import io
import json
import os
import zipfile
from contextlib import closing
from datetime import date, timedelta
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
    with closing(connect_db(directory / 'blog.db')) as conn, conn:
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
    latest = max(artifacts, key=lambda a: (a['created_at'], a['id']))
    archive = github_get(f"/actions/artifacts/{latest['id']}/zip")
    with zipfile.ZipFile(io.BytesIO(archive)) as z:
        encrypted = z.read('bundle.enc')
    extract_bundle(encrypted, directory, os.environ['BLOG_BUNDLE_KEY'])


def import_manual_saves(settings) -> None:
    path = settings.root / 'config/manual-saves.json'
    if not path.exists():
        return
    payload = json.loads(path.read_text())
    with closing(connect_db(settings.db_path)) as conn, conn:
        for record in payload['records']:
            if (record.get('status') != 'SAVED_NAVER' or not record.get('request_id')
                    or record.get('category') not in settings.config['categories']
                    or date.fromisoformat(record['day']) > today_kst()):
                raise ValueError('Invalid manual-save receipt')
            existing = conn.execute('SELECT id FROM attempts WHERE request_id=?',
                                    (record['request_id'],)).fetchone()
            if existing:
                conn.execute("UPDATE attempts SET status='SAVED_NAVER' WHERE request_id=?",
                             (record['request_id'],))
            else:
                conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                             (record['day'], record['category'], record['request_id'], 'SAVED_NAVER'))


def seed_inputs(settings) -> None:
    import_manual_saves(settings)
    seed = json.loads(os.environ.get('BLOG_SEED_JSON', '{}'))
    for item in seed.get('requests', []):
        if not (settings.inbox_dir / item['id']).exists():
            temporary = settings.db_path.parent / 'seed-request.json'
            atomic_json(temporary, item)
            enqueue_file(settings, temporary)
            temporary.unlink()
    with closing(connect_db(settings.db_path)) as conn, conn:
        # Persist already published source identities; never generate them again.
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
        rows = conn.execute("SELECT * FROM posts WHERE status='APPROVED' "
                            "AND as_of_date BETWEEN ? AND ? ORDER BY as_of_date,id",
                            ((today-timedelta(days=3)).isoformat(), today.isoformat())).fetchall()
        for row in rows:
            post = load_post(row)
            packet.append({'id': row['id'], 'category_no': settings.config['categories'][post.category]['naver_category_no'],
                           'requires_fresh_review': post.as_of_date != today.isoformat(),
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
        for path in (directory / 'response-cache').glob('*.json'):
            if path.name[:10] >= cutoff:
                archive.write(path, path.relative_to(directory))
        for name in ['blog.db', 'bundle-info.json', 'ready.json', 'context.json',
                     'topic-cache.json', 'topic-selection.json', 'usage.jsonl', 'run-summary.json']:
            path = directory/name
            if path.exists(): archive.write(path, name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(Fernet(os.environ['BLOG_BUNDLE_KEY'].encode()).encrypt(buffer.getvalue()))


def filter_ready(directory: Path, receipts: dict) -> None:
    """Require today's verified Work ledger; old API state cannot acknowledge saves."""
    today = today_kst()
    if receipts.get('verified_date') != today.isoformat() or not isinstance(receipts.get('records'), list):
        raise ValueError('A freshly verified Work save ledger is required')
    blocked = set()
    for record in receipts['records']:
        if not record.get('request_id') or record.get('status') not in {
            'SAVING', 'SAVE_UNCERTAIN', 'SAVED_NAVER', 'PUBLISHED',
        }:
            raise ValueError('Invalid Work save receipt')
        blocked.add(record['request_id'])
    path = directory / 'ready.json'
    ready = json.loads(path.read_text())
    if ready['date'] != today.isoformat():
        raise ValueError('Stale handoff')
    eligible = []
    for item in ready['posts']:
        post = item['post']
        identity = post['request_id']
        if not identity:
            raise ValueError('Missing request identity')
        if identity in blocked or post['status'] != 'APPROVED':
            continue
        if not (today-timedelta(days=3)).isoformat() <= post['as_of_date'] <= today.isoformat():
            continue
        item['requires_fresh_review'] = post['as_of_date'] != today.isoformat()
        eligible.append(item)
        blocked.add(identity)
    atomic_json(path, {**ready, 'posts': eligible})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['bootstrap', 'prepare', 'recover', 'unpack', 'probe'])
    parser.add_argument('--file', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--receipts', type=Path)
    args = parser.parse_args()
    if args.mode == 'unpack':
        if args.receipts is None:
            parser.error('unpack requires --receipts from the verified Work save ledger')
        receipts = json.loads(args.receipts.read_text())
        extract_bundle(args.file.read_bytes(), args.destination, os.environ['BLOG_BUNDLE_KEY'])
        filter_ready(args.destination, receipts)
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
        if args.mode == 'probe':
            from openai import OpenAI

            from .responses import BENCHMARK_SCHEMA, _get, request_json
            # One bounded public test; no article generation, image charge or Naver write.
            payload, response = request_json(
                OpenAI(api_key=settings.openai_api_key, timeout=120, max_retries=0),
                model=settings.openai_model, stage='benchmark', request_id='diagnostic-probe',
                schema=BENCHMARK_SCHEMA, journal=directory/'usage.jsonl',
                max_output_tokens=6000, reasoning={'effort':'low'},
                max_tool_calls=1, tools=[{'type':'web_search'}], tool_choice='required',
                include=['web_search_call.action.sources'],
                input='네이버 블로그 데드리프트 초보 자세 검색 결과를 확인한다. '
                      '검색은 1회만 한다. 실제 읽은 블로그 글만 records에 URL과 구조 관찰을 넣고, '
                      '열람 불가하면 빈 records와 접근 한계를 limitations에 적는다. JSON으로 답한다.',
            )
            results = [{'status':'PROBE_PASSED', 'response_status':_get(response, 'status'),
                        'records':len(payload['records'])}]
        else:
            results = run_daily(settings, count=3, save_to_naver=False,
                                retry_failed=args.mode == 'recover')
        if args.mode != 'probe':
            with closing(connect_db(settings.db_path)) as conn:
                unresolved = conn.execute(
                    "SELECT COUNT(*) FROM attempts WHERE day=? AND status IN ('ERROR', 'STARTED')",
                    (today_kst().isoformat(),),
                ).fetchone()[0]
            if unresolved:
                results.append({'status': 'UNRESOLVED_FAILED_ATTEMPTS', 'count': unresolved})
        print(json.dumps(results, ensure_ascii=False))
        failed = any(r.get('status') in {'ERROR', 'RESEARCH_REQUIRED', 'IMAGES_PENDING',
                                        'SETUP_REQUIRED', 'MANUAL_CHECK_REQUIRED',
                                        'INPUT_REJECTED', 'COMMUNITY_SOURCE_PENDING',
                                        'COMMUNITY_SOURCE_UNAVAILABLE',
                                        'SAVE_UNCERTAIN', 'RECOVERY_INPUT_UNAVAILABLE',
                                        'UNRESOLVED_FAILED_ATTEMPTS', 'NO_ELIGIBLE_INVESTMENT',
                                        'DROP_REVIEW', 'DROP_DUPLICATE'} for r in results)
        # A no-op/partial run must not report success when a category has no deliverable.
        categories = getattr(settings, 'config', {}).get('categories', {})
        if categories:
            with closing(connect_db(settings.db_path)) as conn:
                ready_categories = {row[0] for row in conn.execute(
                    "SELECT category FROM posts WHERE as_of_date=? AND status IN ('APPROVED','SAVED_NAVER')",
                    (today_kst().isoformat(),))}
                ready_categories.update(row[0] for row in conn.execute(
                    "SELECT category FROM attempts WHERE day=? AND status='SAVED_NAVER'",
                    (today_kst().isoformat(),)))
            missing = sorted(set(categories) - {'cooking'} - ready_categories)
            if missing:
                results.append({'status': 'PREPARATION_PARTIAL', 'missing_categories': missing})
                failed = True
            elif all(r.get('status') in {'APPROVED', 'SAVED_NAVER', 'DROP_REVIEW',
                                         'DROP_DUPLICATE', 'NO_ELIGIBLE_INPUT_OR_DAILY_LIMIT'}
                     for r in results):
                failed = False  # A bounded replacement can resolve an earlier editorial rejection.
        atomic_json(directory / 'run-summary.json',
                    {'date': today_kst().isoformat(), 'failed': failed, 'results': results})
        if failed:
            raise RuntimeError('Preparation did not complete; inspect private diagnostics')
    finally:
        # Checkpoints survive handled API errors; no raw files are uploaded to the public repository.
        pack(settings, destination)
        summary_path = directory / 'run-summary.json'
        if (args.mode != 'probe' and summary_path.exists()
                and json.loads(summary_path.read_text()).get('date') == today_kst().isoformat()):
            from .notify import telegram
            try:
                telegram(json.loads(summary_path.read_text())['results'])
            except (OSError, RuntimeError, ValueError) as exc:
                print(json.dumps({'notification': 'FAILED', 'error': type(exc).__name__}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: redact all errors and exit nonzero.
        print(json.dumps({'status': 'ERROR', 'error': type(exc).__name__}))
        # Restore/setup failures can happen before run-summary exists.
        from .notify import telegram
        try:
            telegram([{'status': 'ERROR', 'stage': 'cloud', 'error': type(exc).__name__}])
        except (OSError, RuntimeError, ValueError) as notify_exc:
            print(json.dumps({'notification': 'FAILED', 'error': type(notify_exc).__name__}))
        raise SystemExit(1) from None
