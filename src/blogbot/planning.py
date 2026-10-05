"""Deterministic KST content slots; no topic invention or external scheduling."""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from datetime import date, datetime
from zoneinfo import ZoneInfo

from .core import KST, today_kst


def resolve_plan(config: dict, day: date | None = None) -> dict | None:
    policy = config.get('operating_plan', {})
    day = day or today_kst()
    if not policy.get('enabled') or day < date.fromisoformat(policy['start_date']):
        return None
    selected = (policy['previous'] if policy.get('effective_date')
                and day < date.fromisoformat(policy['effective_date']) else policy)
    categories = selected['weekdays']
    if (len(categories) != 7 or any(c not in {'parenting', 'exercise', 'investment', 'origins'}
                                   for c in categories)):
        raise ValueError('Invalid weekly content plan')
    category = categories[day.weekday()]
    reservation = policy.get('reservations', {}).get(day.isoformat())
    if reservation and (set(reservation) != {'category', 'kind'}
                        or reservation['category'] != category
                        or reservation['kind'] not in {'existing_owner_draft', 'owner_preparation_pending', 'owner_input_pending'}):
        raise ValueError('Editorial reservation must match the selected date/category')
    # measurement_end is reporting metadata, deliberately not an expiry switch.
    return {'version': selected['version'], 'date': day.isoformat(), 'target': 1,
            'category': category, 'editorial_types': (['article', 'ai_tutorial']
                                                   if category == 'parenting' else ['article']),
            'depth': 'short' if category == 'origins' else 'deep', **({'reservation': dict(reservation)} if reservation else {})}


def active_plan(settings) -> dict | None:
    config = getattr(settings, 'config', {})
    plan = config.get('daily_plan')
    changed = bool(config.get('operating_plan') and resolve_plan(config) != plan)
    if changed or (plan and plan['date'] != today_kst().isoformat()):
        raise ValueError('Reload settings after the KST date changes')
    return plan


def reservation_result(plan: dict) -> dict:
    """An intentional editorial hold, never an API approval or actual-save receipt."""
    return {'status': 'EDITORIAL_SLOT_RESERVED', 'date': plan['date'],
            'category': plan['category'], 'reason': plan['reservation']['kind']}


def editorial_type(category: str, data: dict) -> str:
    kind = data.get('editorial_type', 'article')
    if kind not in {'article', 'ai_tutorial'} or (kind == 'ai_tutorial' and category != 'parenting'):
        raise ValueError('Unsupported editorial subtype')
    return kind


def matches_request(request, plan: dict) -> bool:
    return (request.category == plan['category']
            and editorial_type(request.category, request.data) in plan['editorial_types'])


def matches_post(post, plan: dict) -> bool:
    return (post.as_of_date == plan['date'] and post.category == plan['category']
            and post.provenance.get('daily_plan') == plan
            and post.provenance.get('editorial_type', 'article') in plan['editorial_types'])


class SaveDateRequired(RuntimeError):
    """A historic saved status lacks verified actual-save-date evidence."""


def saved_count(conn, plan: dict) -> int:
    """Use authoritative receipts or aware save timestamps; never generation dates."""
    receipts = {r['request_id']: r['day'] for r in conn.execute('SELECT * FROM save_receipts')}
    identities = {identity for identity, day in receipts.items() if day == plan['date']}
    known = set(receipts)
    unknown = set()
    for row in conn.execute("SELECT id,request_id,draft_saved_at FROM posts WHERE status='SAVED_NAVER'"):
        identity = row['request_id'] or f"post:{row['id']}"
        if identity in receipts:
            continue  # Verified actual day overrides legacy resolution/generation timestamps.
        stamp = row['draft_saved_at']
        try:
            parsed = datetime.fromisoformat(stamp) if stamp else None
            if parsed is None or parsed.tzinfo is None:
                raise ValueError('Unknown timezone or missing save timestamp')
            saved_day = parsed.astimezone(KST).date().isoformat()
        except (TypeError, ValueError):
            unknown.add(identity)
            continue
        known.add(identity)
        if saved_day == plan['date']:
            identities.add(identity)
    unknown.update(r['request_id'] or f"attempt:{r['id']}" for r in conn.execute(
        "SELECT id,request_id FROM attempts WHERE status='SAVED_NAVER'")
        if r['request_id'] not in known)
    if unknown:
        raise SaveDateRequired('Reconcile saved records with verified actual save dates')
    return len(identities)


def attempt_matches(row, plan: dict) -> bool:
    return row['day'] == plan['date'] and json.loads(row['plan_json']) == plan


def ready_categories(settings, conn) -> set[str]:
    plan = active_plan(settings)
    if not plan:
        return {row[0] for row in conn.execute(
            "SELECT category FROM posts WHERE as_of_date=? AND status IN ('APPROVED','SAVED_NAVER') "
            "UNION SELECT category FROM attempts WHERE day=? AND status='SAVED_NAVER'",
            (str(today_kst()), str(today_kst())))}
    from .core import load_post
    if plan.get('reservation'):
        return set()  # Manual editorial coverage does not assert API preparation or saving.
    try:
        count = saved_count(conn, plan)
    except SaveDateRequired:
        return set()
    if count:
        # A verified Work receipt can acknowledge an API post still marked APPROVED.
        saved = saved_identities(conn, plan['date'])
        rows = conn.execute("SELECT * FROM posts WHERE status IN ('APPROVED','SAVED_NAVER')").fetchall()
        matched = any(matches_post(load_post(r), plan) and (
            (r['request_id'] or f"post:{r['id']}") in saved) for r in rows)
        return {plan['category']} if matched else set()
    return {plan['category']} if any(matches_post(load_post(r), plan) for r in conn.execute(
        "SELECT * FROM posts WHERE status='APPROVED' AND as_of_date=?", (plan['date'],))) else set()


def saved_identities(conn, day: str) -> set[str]:
    # attempts.day always remains generation/reservation day, never save evidence.
    receipts = {r['request_id']: r['day'] for r in conn.execute('SELECT * FROM save_receipts')}
    identities = {identity for identity, actual in receipts.items() if actual == day}
    for row in conn.execute("SELECT id,request_id,draft_saved_at FROM posts WHERE status='SAVED_NAVER'"):
        identity = row['request_id'] or f"post:{row['id']}"
        if identity in receipts or not row['draft_saved_at']:
            continue
        stamp = datetime.fromisoformat(row['draft_saved_at'])
        if stamp.tzinfo is not None and stamp.astimezone(KST).date().isoformat() == day:
            identities.add(identity)
    return identities


def receipt_day(record: dict) -> str:
    """Derive only from actual save evidence, never draft date or verified_date."""
    days = set()
    if record.get('day'):
        days.add(date.fromisoformat(record['day']).isoformat())
    if record.get('saved_display') and record.get('saved_display_timezone'):
        stamp = record['saved_display']
        if not isinstance(stamp, str) or not re.fullmatch(r'\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}', stamp):
            raise ValueError('Invalid displayed save timestamp')
        timezone = ZoneInfo(record['saved_display_timezone'])
        local = datetime.strptime(stamp, '%Y.%m.%d %H:%M').replace(tzinfo=timezone)
        if local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset():
            raise ValueError('Ambiguous UI time requires an explicit offset')
        days.add(local.astimezone(KST).date().isoformat())
    # Unlabeled display text is not date evidence. An independently verified day
    # or aware timestamp may still be used; never retrofit a timezone onto UI text.
    for field in ('saved_at', 'draft_saved_at'):
        if record.get(field):
            stamp = datetime.fromisoformat(record[field])
            if stamp.tzinfo is None:
                raise ValueError('Save timestamp requires a timezone')
            days.add(stamp.astimezone(KST).date().isoformat())
    if len(days) != 1:
        raise ValueError('Daily-plan receipts require one consistent actual save day')
    return days.pop()


class MediaBusy(RuntimeError):
    """Another local process owns this paid media stage."""


@contextmanager
def media_claim(directory, request_id):
    """Process-safe advisory lock; crash release preserves existing image cooldowns."""
    folder = directory / 'media-locks'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(request_id.encode()).hexdigest() + '.lock')
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise MediaBusy('Media is already in progress') from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MediaBusy('Media is already in progress') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def record_verified_save(conn, post_id: int, record: dict) -> None:
    """Manual resolution records observed save time/day, not resolution time."""
    day = receipt_day(record)
    if date.fromisoformat(day) > today_kst():
        raise ValueError('Save receipt cannot be future-dated')
    row = conn.execute('SELECT id,request_id,category FROM posts WHERE id=?', (post_id,)).fetchone()
    identity = row['request_id'] or f"post:{row['id']}"
    old = conn.execute('SELECT day FROM save_receipts WHERE request_id=?', (identity,)).fetchone()
    if old and old['day'] != day:
        raise ValueError('Conflicting save day requires reconciliation')
    with conn:
        conn.execute('INSERT OR IGNORE INTO save_receipts(request_id,day,category) VALUES(?,?,?)',
                     (identity, day, row['category']))
        conn.execute("UPDATE posts SET status='SAVED_NAVER',draft_saved_at=? WHERE id=?",
                     (record.get('saved_at'), post_id))
        if row['request_id']:
            conn.execute("UPDATE attempts SET status='SAVED_NAVER' WHERE request_id=?", (identity,))
