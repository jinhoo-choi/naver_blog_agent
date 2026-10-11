"""API-only preparation and encrypted handoff; Work records saving/publication separately."""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.fernet import Fernet

from .config import load_settings
from .core import (
    KST,
    PUBLICATION_FINISHED,
    PUBLICATION_PENDING,
    WORK_RECEIPT_STATES,
    connect_db,
    import_work_receipts,
    merge_work_receipts,
    today_kst,
    validate_work_receipt,
)
from .images import atomic_json
from .inputs import enqueue_file, managed_queue_photos
from .investment import revalidation_reason
from .pipeline import run_daily
from .planning import (
    SaveDateRequired,
    active_plan,
    matches_post,
    ready_categories,
    receipt_day,
    reservation_result,
    resolve_plan,
    saved_count,
)
from .presentation import render_segments


class PreparationFailed(RuntimeError):
    """A persisted preparation failure whose detailed notification was already attempted."""


MAX_ENCRYPTED_BUNDLE_BYTES = 55_000_000  # Below the 60 MB artifact reader, including ZIP overhead.


def recover_preparation(settings, results: list[dict], count: int) -> list[dict]:
    """Resume missing work with a durable daily budget, including explicit recover runs."""
    plan = active_plan(settings)
    if plan and plan.get('reservation'):
        return [reservation_result(plan)]
    path = settings.db_path.parent / 'auto-recovery.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    attempts = (int(state.get('attempts', int(bool(state.get('attempted')))))
                if state.get('date') == str(today_kst()) else 0)
    while attempts < 2 and any(
        r.get('status') in {'DROP_REVIEW', 'RESEARCH_REQUIRED', 'IMAGES_PENDING',
                            'COMMUNITY_SOURCE_PENDING', 'COMMUNITY_SOURCE_UNAVAILABLE'} or
        (r.get('status') == 'ERROR' and r.get('reason') in {
            'unobserved_source_url', 'unobserved_body_url', 'invalid_reference_date',
            'invalid_preview', 'missing_headings', 'body_too_short',
            'editorial_recovery_failed', 'cached_response_unavailable',
        }) for r in results
    ):
        categories = settings.config['categories']
        expected = {key for key, info in categories.items()
                    if key != 'cooking' and info.get('max_daily', 1) > 0}
        with closing(connect_db(settings.db_path)) as conn:
            ready = ready_categories(settings, conn)
        if expected <= ready:
            break
        attempts += 1
        # Claim before paid work; workflow reruns and restored checkpoints share this limit.
        atomic_json(path, {'date': str(today_kst()), 'attempted': True, 'attempts': attempts})
        print(json.dumps({'status': 'PREPARATION_RETRY', 'attempt': attempts,
                          'maximum': 2}), flush=True)
        retried = run_daily(settings, count=count, save_to_naver=False, retry_failed=True)
        replaced = {r['request_id'] for r in retried if r.get('request_id')}
        approved = {r.get('category') for r in retried if r.get('status') in {'APPROVED', 'SAVED_NAVER'}}
        results = [r for r in results if r.get('request_id') not in replaced
                   and r.get('category') not in approved] + retried
    return results


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
    db_path = directory / 'blog.db'
    preserved = []
    if db_path.exists():
        # Read before extracting anything. Corrupt/missing tables are errors, never an empty ledger.
        with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Existing Work database is damaged; reconcile before restoring')
            preserved = [dict(row) for row in conn.execute('SELECT * FROM save_receipts')]
            for record in preserved:
                record.setdefault('status', 'SAVED_NAVER')  # Existing saved-only schema.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            target = (directory / info.filename).resolve()
            if not target.is_relative_to(directory.resolve()) or info.file_size > 25_000_000:
                raise ValueError('Invalid bundle member')
            if target == db_path.resolve() and info.filename != 'blog.db':
                raise ValueError('Invalid database bundle member')
        # Never rewind a local one-shot manual claim with an older encrypted bundle.
        manual_files = list((directory / 'manual-requests').glob('**/*'))
        manual_files += list((directory / 'manual-thumbnails').glob('*/*'))
        for path in manual_files:
            if (not path.is_file() or (path.name not in {
                    'claim.json', 'paid-calls.json', 'paid-call.json', 'approved-packet.json',
                    'original-claim.json'} and 'review-once' not in path.parts)):
                continue
            if path.name == 'result.json':
                continue  # Photo path relocation is validated by the result digest.
            name = str(path.relative_to(directory))
            if name not in archive.namelist() or archive.read(name) != path.read_bytes():
                raise ValueError('Manual request checkpoint conflict; preserve existing state')
        preserve_manual_receipts = set()
        for path in (directory / 'manual-requests').glob('*/save-receipt.json'):
            name = str(path.relative_to(directory))
            old = json.loads(path.read_text())
            rank = {'SAVING': 0, 'SAVE_UNCERTAIN': 1, 'SAVED_NAVER': 2, 'SAVE_NOT_SAVED': 2}
            if old.get('status') not in rank:
                raise ValueError('Invalid local additional receipt')
            if name not in archive.namelist():
                preserve_manual_receipts.add(name)
                continue
            incoming = json.loads(archive.read(name))
            if (any(old.get(k) != incoming.get(k) for k in
                    ('request_id', 'day', 'approval_reference', 'result_sha256'))
                    or incoming.get('status') not in rank
                    or (rank[old['status']] == rank.get(incoming.get('status')) == 2
                        and old['status'] != incoming['status'])):
                raise ValueError('Conflicting additional receipt identity/date')
            if rank[old['status']] >= rank[incoming['status']]:
                preserve_manual_receipts.add(name)
        if archive.namelist().count('blog.db') != 1:
            raise ValueError('Bundle requires exactly one database')
        # Reconcile in a temporary DB first so stale bundles or conflicting evidence cannot
        # erase a local publication claim, even when the external Work ledger is replayed.
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=directory) as temporary:
            staged = Path(temporary) / 'blog.db'
            staged.write_bytes(archive.read('blog.db'))
            with closing(connect_db(staged)) as conn:
                merge_work_receipts(conn, preserved, categories={r['category'] for r in preserved})
            archive.extractall(directory, members=[name for name in archive.namelist()
                                                  if name != 'blog.db' and name not in preserve_manual_receipts])
            os.replace(staged, db_path)
    metadata = json.loads((directory / 'bundle-info.json').read_text())
    old_root = Path(metadata['data_root'])
    def rebase(photos):
        for photo in photos:
            relative = Path(photo['file']).relative_to(old_root)
            target = (directory / relative).resolve()
            if not target.is_relative_to(directory.resolve()):
                raise ValueError('Photo path escapes restored bundle')
            photo['file'] = str(target)
    for path in (directory / 'inbox').glob('*/request.json'):
        try:
            request = json.loads(path.read_text())
            photos = request['photos']
            if not isinstance(photos, list):
                raise TypeError('Invalid queue photos')
        except (ValueError, TypeError, KeyError):
            continue  # Preserve rejected input bytes; do not reinterpret a damaged queue.
        rebase(photos)
        atomic_json(path, request)
    for path in (directory / 'drafts').glob('*.json'):
        try:
            payload = json.loads(path.read_text())
            post_photos, input_photos = payload['post']['photos'], payload['input']['photos']
            if not isinstance(post_photos, list) or not isinstance(input_photos, list):
                raise TypeError('Invalid packet photo lists')
        except (ValueError, TypeError, KeyError):
            # Retain damaged bytes for reconciliation; pending media cannot use them.
            # Path validation in rebase remains fail-closed for parseable packets.
            continue
        rebase(post_photos)
        rebase(input_photos)
        atomic_json(path, payload)
    for path in (directory / 'manual-requests').glob('**/result.json'):
        payload = json.loads(path.read_text())
        rebase(payload['post']['photos'])
        rebase(payload['input']['photos'])
        atomic_json(path, payload)
    for path in (directory / 'manual-thumbnails').glob('*/result.json'):
        payload = json.loads(path.read_text())
        rebase([payload['photo']])
        atomic_json(path, payload)
    ready_path = directory / 'ready.json'
    if ready_path.exists():
        ready = json.loads(ready_path.read_text())
        for item in ready.get('posts', []) + ready.get('publication_ready', []):
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
        conn.execute('BEGIN IMMEDIATE')
        for record in payload['records']:
            day = receipt_day(record)
            if (record.get('status') != 'SAVED_NAVER' or not record.get('request_id')
                    or record.get('category') not in settings.config['categories']
                    or date.fromisoformat(day) > today_kst()):
                raise ValueError('Invalid manual-save receipt')
            receipt = conn.execute('SELECT day,category,status FROM save_receipts WHERE request_id=?',
                                   (record['request_id'],)).fetchone()
            if receipt and (receipt['day'] != day or receipt['category'] != record['category']):
                raise ValueError('Conflicting manual-save receipt; reconcile without resetting budgets')
            if receipt and receipt['status'] in PUBLICATION_PENDING | PUBLICATION_FINISHED | {'SAVE_NOT_SAVED'}:
                continue  # A legacy saved-only ledger cannot erase observed publication state.
            post = conn.execute('SELECT category,status FROM posts WHERE request_id=?',
                                (record['request_id'],)).fetchone()
            if post and (post['category'] != record['category'] or post['status'] in
                         PUBLICATION_PENDING | PUBLICATION_FINISHED | {'SAVE_NOT_SAVED'}):
                raise ValueError('Saved-only receipt conflicts with observed post state')
            conn.execute("INSERT INTO save_receipts(request_id,day,category,status) VALUES(?,?,?,'SAVED_NAVER') "
                         "ON CONFLICT(request_id) DO UPDATE SET status='SAVED_NAVER'",
                         (record['request_id'], day, record['category']))
            conn.execute("UPDATE posts SET status='SAVED_NAVER' WHERE request_id=?", (record['request_id'],))
            existing = conn.execute('SELECT id,category FROM attempts WHERE request_id=?',
                                    (record['request_id'],)).fetchone()
            if existing:
                if existing['category'] != record['category']:
                    raise ValueError('Receipt category does not match the original request')
                conn.execute("UPDATE attempts SET status='SAVED_NAVER' WHERE request_id=?",
                             (record['request_id'],))
            else:
                conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                             (day, record['category'], record['request_id'], 'SAVED_NAVER'))


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
    packet, weekly_holds = [], []
    config = getattr(settings, 'config', {})
    plan = resolve_plan(config, today) if config.get('operating_plan') else active_plan(settings)
    stale_settings = config.get('daily_plan') != plan
    # A midnight boundary must hold delivery, never prevent checkpoint transport.
    # Render transport segments; the saver can paste them without another generation.
    from .core import load_post
    with closing(connect_db(settings.db_path)) as conn:
        pending_ids = {r['request_id'] for r in conn.execute(
            "SELECT request_id FROM posts WHERE status IN ('TEXT_APPROVED','IMAGES_PENDING')")}
        rows = conn.execute("SELECT * FROM posts WHERE status='APPROVED' "
                            "AND as_of_date BETWEEN ? AND ? ORDER BY as_of_date,id",
                            ((today-timedelta(days=3)).isoformat(), today.isoformat())).fetchall()
        uncertain = conn.execute("SELECT 1 FROM posts WHERE status IN ('SAVING','SAVE_UNCERTAIN') LIMIT 1").fetchone()
        try:
            used = saved_count(conn, plan) if plan else 0
        except SaveDateRequired:
            used = 1
            uncertain = True
        for row in rows:
            post = load_post(row)
            number = settings.config['categories'][post.category].get('naver_category_no')
            if type(number) is not int or number < 1:
                continue
            if stale_settings or (plan and (plan.get('reservation') or uncertain or not matches_post(post, plan)
                                           or packet or used)):
                continue
            from .investment import validate_current_post
            try:
                validate_current_post(settings, post)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                weekly_holds.append({'id': row['id'], 'request_id': post.request_id,
                                     'category': 'investment',
                                     'status': revalidation_reason(plan).upper()})
                continue  # Preserve checkpoints while withholding unverified weekly output.
            packet.append({'id': row['id'], 'category_no': settings.config['categories'][post.category]['naver_category_no'],
                           'requires_fresh_review': post.as_of_date != today.isoformat(),
                           'post': post.__dict__, 'segments': [s.__dict__ for s in render_segments(
                               post, include_tags=post.provenance.get('content_style') != 'review')]})
    hold = (revalidation_reason(plan) if weekly_holds else
            'kst_date_changed' if stale_settings else 'editorial_slot_reserved'
            if plan and plan.get('reservation') else None)
    atomic_json(directory / 'ready.json', {'date': today.isoformat(), 'posts': packet,
                                          **({'daily_plan': plan} if plan else {}),
                                          **({'hold': hold} if hold else {})})
    if weekly_holds:
        summary_path = directory / 'run-summary.json'
        summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}
        prior = summary.get('results', []) if summary.get('date') == str(today) else []
        held_ids = {r['id'] for r in weekly_holds}
        results = [r for r in prior if r.get('id') not in held_ids
                   and r.get('status') != 'DAILY_PLAN_READY'] + weekly_holds
        atomic_json(summary_path, {'date': str(today), 'failed': True, 'results': results})
    atomic_json(directory / 'bundle-info.json', {'data_root': str(directory.resolve()),
                                                'date': today.isoformat(), 'version': 1})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        # Retain state and recent packets; old completed media are not transported forever.
        keep_ids = set(pending_ids)
        for path in settings.artifact_dir.glob('*.json'):
            try:
                payload = json.loads(path.read_text())
                keep = (payload['post']['as_of_date'] >= cutoff
                        or path.name.startswith('manual-images-'))
                request_id = payload['post']['request_id']
                if not isinstance(request_id, str):
                    raise TypeError('Invalid packet request id')
            except (ValueError, TypeError, KeyError):
                # Preserve evidence, never silently drop the broken packet or paid-call state.
                archive.write(path, path.relative_to(directory))
                continue
            if keep or request_id in pending_ids:
                keep_ids.add(request_id)
                archive.write(path, path.relative_to(directory))
                md = path.with_suffix('.md')
                if md.exists(): archive.write(md, md.relative_to(directory))
        for request_id in keep_ids:
            for path in (settings.artifact_dir/'generated-images'/request_id).glob('*'):
                if path.is_file() and path.suffix != '.tmp':
                    archive.write(path, path.relative_to(directory))
        for path in settings.inbox_dir.glob('*/request.json'):
            archive.write(path, path.relative_to(directory))
        # Only managed supplied-image filenames, inside the encrypted bundle.
        # A review must not lose its evidence when the next runner restores state.
        for path in managed_queue_photos(settings):
            archive.write(path, path.relative_to(directory))
        for path in (directory / 'response-cache').glob('*.json'):
            if path.name[:10] >= cutoff:
                archive.write(path, path.relative_to(directory))
        for path in (directory / 'policy-evidence').glob('*.json'):
            if (path.is_symlink() or not re.fullmatch(r'[a-f0-9]{64}\.json', path.name)
                    or path.stat().st_size > 100_000):
                raise ValueError('Invalid managed policy evidence file')
            archive.write(path, path.relative_to(directory))
        for path in (directory / 'source-evidence').glob('*.json'):
            archive.write(path, path.relative_to(directory))
        for path in (directory / 'manual-requests').glob('**/*'):
            relative = path.relative_to(directory / 'manual-requests')
            parts = relative.parts
            slug = re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', parts[0])
            if path.is_dir() and not path.is_symlink():
                if not slug or not (len(parts) == 1 or (len(parts) == 2 and parts[1] == 'review-once')
                                    or (len(parts) == 3 and parts[1:] == ('review-once', 'response-cache'))):
                    raise ValueError('Invalid manual request checkpoint directory')
                continue
            valid = (slug and ((len(parts) == 2 and path.name in {
                'claim.json', 'approved-packet.json', 'paid-calls.json', 'result.json',
                'save-receipt.json', 'thumbnail.jpg', 'thumbnail.png', 'thumbnail.webp'})
                or (len(parts) == 3 and parts[1] == 'review-once' and path.name in {
                    'claim.json', 'original-claim.json', 'approved-packet.json',
                    'paid-calls.json', 'usage.jsonl', 'result.json'})
                or (len(parts) == 4 and parts[1:3] == ('review-once', 'response-cache')
                    and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.json', path.name))))
            if (not valid or path.is_symlink() or not path.is_file()
                    or path.stat().st_size > 5_000_000):
                raise ValueError('Invalid manual request checkpoint')
            archive.write(path, path.relative_to(directory))
        for path in (directory / 'manual-thumbnails').glob('**/*'):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(directory / 'manual-thumbnails')
            valid = (len(relative.parts) in (2, 3)
                     and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', relative.parts[0])
                     and ((len(relative.parts) == 2 and path.name in {
                         'claim.json', 'approved-packet.json', 'paid-call.json', 'result.json'})
                          or (len(relative.parts) == 3 and relative.parts[1] == 'image'
                              and (path.name == '.image-plan' or re.fullmatch(
                                  r'[a-f0-9]{64}\.(?:json|jpg)', path.name)))))
            if (not valid or path.is_symlink() or not path.is_file()
                    or path.stat().st_size > 5_000_000):
                raise ValueError('Invalid manual thumbnail checkpoint')
            archive.write(path, path.relative_to(directory))
        for name in ['blog.db', 'bundle-info.json', 'ready.json', 'context.json',
                     'topic-cache.json', 'topic-selection.json', 'creator-trends.json',
                     'creator-trend-selection.json', 'creator-trend-decisions.json',
                     'usage.jsonl', 'run-summary.json',
                     'auto-recovery.json']:
            path = directory/name
            if path.exists(): archive.write(path, name)
    encrypted = Fernet(os.environ['BLOG_BUNDLE_KEY'].encode()).encrypt(buffer.getvalue())
    if len(encrypted) > MAX_ENCRYPTED_BUNDLE_BYTES:
        raise ValueError('Bundle transport capacity exceeded; existing destination preserved')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encrypted)
    return hold


def filter_ready(directory: Path, receipts: dict, plan: dict | None = None, settings=None) -> None:
    """Require today's verified Work ledger; old API state cannot acknowledge saves."""
    today = today_kst()
    if receipts.get('verified_date') != today.isoformat() or not isinstance(receipts.get('records'), list):
        raise ValueError('A freshly verified Work save ledger is required')
    blocked = set()
    used = set()
    uncertain = False
    records = list(receipts['records'])
    if settings is not None:
        with closing(connect_db(settings.db_path)) as conn:
            # Empty/replayed input cannot erase a locally recorded publication attempt.
            records.extend(dict(r) for r in conn.execute('SELECT * FROM save_receipts'))
    for record in records:
        if not record.get('request_id') or record.get('status') not in WORK_RECEIPT_STATES:
            raise ValueError('Invalid Work save receipt')
        if record['status'] in PUBLICATION_PENDING | PUBLICATION_FINISHED | {'SAVE_NOT_SAVED'}:
            owner = (getattr(settings, 'naver_blog_id', '') or
                     getattr(settings, 'config', {}).get('creator_advisor', {}).get('channel_id', ''))
            record = validate_work_receipt(record, owner)
            uncertain |= record['status'] in PUBLICATION_PENDING
            from datetime import datetime
            for field in ('published_at', 'publish_attempted_at'):
                if record.get(field) and datetime.fromisoformat(record[field]).astimezone(KST).date() == today:
                    used.add(record['request_id'])
        blocked.add(record['request_id'])
        if plan:
            try:
                day = date.fromisoformat(receipt_day(record))
            except (KeyError, TypeError, ValueError):
                raise ValueError('Daily-plan receipts require the actual save day') from None
            if day > today:
                raise ValueError('Receipt cannot be future-dated')
            if day == today:
                used.add(record['request_id'])
            uncertain |= record['status'] in {'SAVING', 'SAVE_UNCERTAIN'}
    path = directory / 'ready.json'
    ready = json.loads(path.read_text())
    if ready['date'] != today.isoformat():
        raise ValueError('Stale handoff')
    if ready.get('daily_plan') != plan or (plan and plan['date'] != today.isoformat()):
        raise ValueError('Handoff does not match the current daily plan')
    eligible, publication_ready, weekly_held = [], [], False
    observed_saved = {r['request_id'] for r in records if r['status'] == 'SAVED_NAVER'
                      and (not plan or receipt_day(r) == plan['date'])}
    observed_saved -= {r['request_id'] for r in records if r['status'] != 'SAVED_NAVER'}
    for item in ready['posts'] + ready.get('publication_ready', []):
        post = item['post']
        identity = post['request_id']
        publication_candidate = (plan and identity in observed_saved and not publication_ready
                                 and used <= {identity})
        if uncertain and any(r.get('status') in PUBLICATION_PENDING for r in records):
            continue
        if plan and (plan.get('reservation') or uncertain or (used and not publication_candidate)
                     or eligible or post['as_of_date'] != plan['date']
                     or post['category'] != plan['category']
                     or post.get('provenance', {}).get('daily_plan') != plan):
            continue
        if plan and plan.get('investment_mode'):
            from .core import PostDraft
            from .investment import validate_current_post
            if settings is None or not matches_post(PostDraft(**post), plan):
                weekly_held = True
                continue
            try:
                validate_current_post(settings, PostDraft(**post))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                weekly_held = True
                continue
        if not identity:
            raise ValueError('Missing request identity')
        if publication_candidate and post['status'] == 'APPROVED':
            publication_ready.append(item)
            continue  # Saved once already: expose only to the separate publication claim.
        if identity in blocked or post['status'] != 'APPROVED':
            continue
        if not (today-timedelta(days=3)).isoformat() <= post['as_of_date'] <= today.isoformat():
            continue
        item['requires_fresh_review'] = post['as_of_date'] != today.isoformat()
        eligible.append(item)
        blocked.add(identity)
    atomic_json(path, {**ready, 'posts': eligible, 'publication_ready': publication_ready,
                       **({'hold': revalidation_reason(plan)} if weekly_held else {})})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['bootstrap', 'prepare', 'recover', 'unpack', 'probe',
                                         'manual-images', 'manual-request', 'manual-request-reserve',
                                         'manual-thumbnail', 'manual-thumbnail-reserve', 'manual-review', 'manual-review-reserve'])
    parser.add_argument('--file', type=Path)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--receipts', type=Path)
    parser.add_argument('--attribution-review-request-id', default='')
    parser.add_argument('--manual-image-request-id', default='')
    parser.add_argument('--manual-request-id', default='')
    parser.add_argument('--manual-request-sha256', default='')
    args = parser.parse_args()
    if ((bool(args.manual_request_id) and bool(args.manual_request_sha256))
            != (args.mode in {'manual-request', 'manual-request-reserve', 'manual-thumbnail', 'manual-thumbnail-reserve', 'manual-review', 'manual-review-reserve'})
            or (args.mode not in {'manual-request', 'manual-request-reserve', 'manual-thumbnail', 'manual-thumbnail-reserve', 'manual-review', 'manual-review-reserve'} and
                (args.manual_request_id or args.manual_request_sha256))):
        parser.error('manual request mode requires exact ID and packet digest')
    if bool(args.manual_image_request_id) != (args.mode == 'manual-images'):
        parser.error('manual image mode requires its explicit request selector')
    if args.attribution_review_request_id and (
            args.mode != 'recover' or not re.fullmatch(
                r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', args.attribution_review_request_id)):
        parser.error('attribution review selector requires recover and one valid existing request ID')
    if args.mode == 'unpack':
        if args.receipts is None:
            parser.error('unpack requires --receipts from the verified Work save ledger')
        receipts = json.loads(args.receipts.read_text())
        extract_bundle(args.file.read_bytes(), args.destination, os.environ['BLOG_BUNDLE_KEY'])
        from dataclasses import replace
        settings = load_settings()
        restored = replace(settings, db_path=args.destination / 'blog.db',
                           artifact_dir=args.destination / 'drafts', inbox_dir=args.destination / 'inbox')
        owner = settings.naver_blog_id or settings.config.get('creator_advisor', {}).get('channel_id', '')
        from .manual_requests import reconcile_additional_receipts
        reconcile_additional_receipts(restored, receipts)
        with closing(connect_db(restored.db_path)) as conn:
            import_work_receipts(conn, receipts, blog_id=owner, categories=settings.config['categories'])
        filter_ready(args.destination, receipts, active_plan(settings), restored)
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
        if args.mode in {'manual-review', 'manual-review-reserve'}:
            from .manual_review import reserve_review, run_review
            handler = reserve_review if args.mode.endswith('-reserve') else run_review
            result = handler(settings, os.environ.get('BLOG_MANUAL_REQUEST_PACKET', ''),
                             args.manual_request_id, args.manual_request_sha256)
            print(json.dumps(result))
            if result['status'] not in {'MANUAL_DRAFT_READY', 'MANUAL_REVIEW_RESERVED'}:
                raise PreparationFailed('Manual review held; reconcile private checkpoints')
            return
        if args.mode in {'manual-thumbnail', 'manual-thumbnail-reserve'}:
            from .manual_thumbnail import reserve_thumbnail, run_thumbnail
            handler = reserve_thumbnail if args.mode.endswith('-reserve') else run_thumbnail
            result = handler(settings, os.environ.get('BLOG_MANUAL_REQUEST_PACKET', ''),
                             args.manual_request_id, args.manual_request_sha256)
            print(json.dumps(result))
            if result['status'] not in {'MANUAL_THUMBNAIL_READY', 'MANUAL_THUMBNAIL_RESERVED'}:
                raise PreparationFailed('Manual thumbnail held; reconcile private checkpoints')
            return
        if args.mode == 'manual-request-reserve':
            from .manual_requests import reserve_manual_request
            result = reserve_manual_request(settings, os.environ.get('BLOG_MANUAL_REQUEST_PACKET', ''),
                                            args.manual_request_id, args.manual_request_sha256)
            print(json.dumps(result))
            return
        if args.mode == 'manual-request':
            from .manual_requests import run_manual_request
            result = run_manual_request(settings, os.environ.get('BLOG_MANUAL_REQUEST_PACKET', ''),
                                        args.manual_request_id, args.manual_request_sha256)
            print(json.dumps(result))
            if result['status'] != 'MANUAL_DRAFT_READY':
                raise PreparationFailed('Manual request held; reconcile private checkpoints')
            return
        if args.mode == 'manual-images':
            from .manual_images import run_manual_images
            result = run_manual_images(settings, os.environ.get('BLOG_MANUAL_IMAGE_PACKET', ''),
                                       args.manual_image_request_id)
            print(json.dumps(result))
            if result['status'] != 'MANUAL_IMAGES_READY':
                raise PreparationFailed('Manual images pending; reconcile private checkpoints')
            return
        plan = active_plan(settings)
        reserved = bool(args.mode in {'prepare', 'recover'} and plan and plan.get('reservation'))
        if not reserved and not args.attribution_review_request_id:
            seed_inputs(settings)
            from .weekly_policy import import_environment
            import_environment(settings, os.environ.get('BLOG_POLICY_EVIDENCE_JSON', ''))
        elif not reserved and args.attribution_review_request_id:
            # Import authoritative save receipts without seeding any candidates or context.
            import_manual_saves(settings)
        if reserved:
            results = [reservation_result(plan)]
        elif args.mode == 'probe':
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
            daily_target = min(getattr(settings, 'daily_count', 3),
                               getattr(settings, 'config', {}).get('blog', {}).get('daily_max', 3))
            explicit = ({'attribution_review_request_id': args.attribution_review_request_id}
                        if args.attribution_review_request_id else {})
            results = run_daily(settings, count=daily_target, save_to_naver=False,
                                retry_failed=args.mode == 'recover', **explicit)
            if not explicit and getattr(settings, 'config', {}).get('categories'):
                results = recover_preparation(settings, results, daily_target)
        if args.mode != 'probe' and not reserved and not args.attribution_review_request_id:
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
                                        'SAVE_UNCERTAIN', 'RECOVERY_INPUT_UNAVAILABLE', 'PLANNED_INPUT_REQUIRED',
                                        'UNRESOLVED_FAILED_ATTEMPTS', 'NO_ELIGIBLE_INVESTMENT',
                                        'DROP_REVIEW', 'DROP_DUPLICATE'} for r in results)
        # A no-op/partial run must not report success when a category has no deliverable.
        categories = getattr(settings, 'config', {}).get('categories', {})
        if categories and not reserved and not args.attribution_review_request_id:
            with closing(connect_db(settings.db_path)) as conn:
                ready = ready_categories(settings, conn)
            expected = {key for key, info in categories.items()
                        if key != 'cooking' and info.get('max_daily', 1) > 0}
            missing = sorted(expected - ready)
            if missing:
                results.append({'status': 'PREPARATION_PARTIAL', 'missing_categories': missing})
                failed = True
            elif all(r.get('status') in {'APPROVED', 'SAVED_NAVER', 'DROP_REVIEW',
                                         'DROP_DUPLICATE', 'NO_ELIGIBLE_INPUT_OR_DAILY_LIMIT',
                                         'DAILY_PLAN_READY', 'DAILY_PLAN_LIMIT'}
                     for r in results):
                failed = False  # A bounded replacement can resolve an earlier editorial rejection.
        atomic_json(directory / 'run-summary.json',
                    {'date': today_kst().isoformat(), 'failed': failed, 'results': results})
        if failed:
            raise PreparationFailed('Preparation did not complete; inspect private diagnostics')
    finally:
        # Checkpoints survive handled API errors; no raw files are uploaded to the public repository.
        delivery_hold = pack(settings, destination)
        summary_path = directory / 'run-summary.json'
        if (args.mode not in {'probe', 'manual-images', 'manual-request', 'manual-request-reserve',
                              'manual-thumbnail', 'manual-thumbnail-reserve', 'manual-review', 'manual-review-reserve'}
                and summary_path.exists()
                and json.loads(summary_path.read_text()).get('date') == today_kst().isoformat()):
            from .notify import telegram
            try:
                telegram(json.loads(summary_path.read_text())['results'])
            except (OSError, RuntimeError, ValueError) as exc:
                print(json.dumps({'notification': 'FAILED', 'error': type(exc).__name__}))
        if delivery_hold in {'weekly_policy_revalidation_required', 'life_economics_revalidation_required'}:
            raise PreparationFailed('Investment source revalidation blocked delivery')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # noqa: BLE001 -- CLI boundary: redact all errors and exit nonzero.
        print(json.dumps({'status': 'ERROR', 'error': type(exc).__name__}))
        # Restore/setup failures can happen before run-summary exists.
        from .notify import telegram
        if not isinstance(exc, PreparationFailed):
            try:
                telegram([{'status': 'ERROR', 'stage': 'cloud', 'error': type(exc).__name__}])
            except (OSError, RuntimeError, ValueError) as notify_exc:
                print(json.dumps({'notification': 'FAILED', 'error': type(notify_exc).__name__}))
        raise SystemExit(1) from None
