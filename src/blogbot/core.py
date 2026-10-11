from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

KST = timezone(timedelta(hours=9))
WORK_RECEIPT_STATES = frozenset({'SAVING', 'SAVE_UNCERTAIN', 'SAVED_NAVER',
                               'SAVE_NOT_SAVED', 'PUBLISHING', 'PUBLISH_UNCERTAIN', 'PUBLISHED',
                               'PUBLICATION_NOT_PUBLISHED'})
PUBLICATION_PENDING = frozenset({'PUBLISHING', 'PUBLISH_UNCERTAIN'})
PUBLICATION_FINISHED = frozenset({'PUBLISHED', 'PUBLICATION_NOT_PUBLISHED'})


def today_kst() -> date:
    return datetime.now(KST).date()


@dataclass
class PostDraft:
    category: str
    subcategory: str
    topic: str
    title: str
    body: str
    tags: list[str]
    source_urls: list[str]
    as_of_date: str
    quality_score: int = 0
    status: str = "DRAFTED"
    request_id: str = ""
    photos: list[dict] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        normalized = re.sub(r"\s+", " ", f"{self.title} {self.body}").strip().lower()
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category TEXT NOT NULL,
          subcategory TEXT NOT NULL,
          topic TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          tags_json TEXT NOT NULL,
          sources_json TEXT NOT NULL,
          as_of_date TEXT NOT NULL,
          quality_score INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          fingerprint TEXT NOT NULL UNIQUE,
          created_at TEXT NOT NULL,
          draft_saved_at TEXT
        )
        """
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS attempts ("
        "id INTEGER PRIMARY KEY, day TEXT NOT NULL, category TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'STARTED')"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS save_receipts ("
                 "request_id TEXT PRIMARY KEY, day TEXT NOT NULL, category TEXT NOT NULL)")
    for table, columns in {
        "posts": {"request_id": "TEXT NOT NULL DEFAULT ''",
                  "photos_json": "TEXT NOT NULL DEFAULT '[]'",
                  "provenance_json": "TEXT NOT NULL DEFAULT '{}'"},
        "attempts": {"request_id": "TEXT", "plan_json": "TEXT NOT NULL DEFAULT '{}'",
                     "event_key": "TEXT"},
        "save_receipts": {"status": "TEXT NOT NULL DEFAULT 'SAVED_NAVER'",
                          "published_url": "TEXT NOT NULL DEFAULT ''",
                          "post_id": "TEXT NOT NULL DEFAULT ''",
                          "published_at": "TEXT NOT NULL DEFAULT ''",
                          "published_at_precision": "TEXT NOT NULL DEFAULT 'second'",
                          "publication_check_json": "TEXT NOT NULL DEFAULT '{}'",
                          "save_check_json": "TEXT NOT NULL DEFAULT '{}'",
                          "publish_attempted_at": "TEXT NOT NULL DEFAULT ''"},
    }.items():
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_per_request "
                 "ON attempts(request_id) WHERE request_id IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_per_event "
                 "ON attempts(event_key) WHERE event_key IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_receipt_per_publication "
                 "ON save_receipts(published_url) WHERE published_url != ''")
    conn.commit()
    return conn


def validate_work_receipt(record: dict, blog_id: str = '') -> dict:
    """Validate observed Work evidence; this never checks a site or publishes a post."""
    from .planning import receipt_day

    if (not isinstance(record, dict) or not isinstance(record.get('request_id'), str)
            or not record['request_id'] or record.get('status') not in WORK_RECEIPT_STATES):
        raise ValueError('Invalid Work receipt')
    result = dict(record)
    result['day'] = receipt_day(record)
    if date.fromisoformat(result['day']) > today_kst():
        raise ValueError('Receipt cannot be future-dated')
    status = result['status']
    if status == 'SAVE_NOT_SAVED':
        check = record.get('save_check') or json.loads(record.get('save_check_json', '{}'))
        if (not isinstance(check, dict) or check.get('resolution') not in {'not-saved', 'discard'}
                or any(check.get(key) is not True for key in
                       ('draft_list_checked', 'published_list_checked', 'draft_absent', 'published_absent'))
                or not isinstance(check.get('evidence_ref'), str) or not 1 <= len(check['evidence_ref']) <= 500):
            raise ValueError('Not-saved resolution requires actual draft and publication absence evidence')
        attempted = datetime.fromisoformat(check.get('attempted_at', ''))
        checked = datetime.fromisoformat(check.get('checked_at', ''))
        parsed = urlsplit(check.get('blog_url', ''))
        match = re.fullmatch(r'/([A-Za-z0-9_-]+)/?', parsed.path)
        if (attempted.tzinfo is None or checked.tzinfo is None or checked < attempted
                or checked > datetime.now(KST) or attempted.astimezone(KST).date().isoformat() != result['day']
                or parsed.scheme != 'https' or parsed.netloc not in {'blog.naver.com', 'm.blog.naver.com'}
                or parsed.query or parsed.fragment or not match or (blog_id and match[1] != blog_id)
                or record.get('publish_attempted_at')):
            raise ValueError('Not-saved resolution must observe the owner after the original save attempt')
        result['save_check_json'] = json.dumps(check, sort_keys=True, ensure_ascii=False)
    stamps = {}
    for stamp_key in ('published_at', 'publish_attempted_at'):
        value = record.get(stamp_key)
        if value:
            stamp = datetime.fromisoformat(value)
            if (stamp.tzinfo is None or stamp > datetime.now(KST)
                    or stamp.astimezone(KST).date().isoformat() < result['day']):
                raise ValueError('Publication timestamps require an observed nonfuture time and timezone')
            stamps[stamp_key] = stamp
            result[stamp_key] = stamp.isoformat()
    if status in PUBLICATION_PENDING | {'PUBLICATION_NOT_PUBLISHED'} and 'publish_attempted_at' not in stamps:
        raise ValueError('Publication attempt time is required')
    if status == 'PUBLICATION_NOT_PUBLISHED':
        check = record.get('publication_check') or json.loads(record.get('publication_check_json', '{}'))
        if (not isinstance(check, dict) or check.get('published_list_checked') is not True
                or check.get('draft_still_saved') is not True
                or not isinstance(check.get('evidence_ref'), str) or not 1 <= len(check['evidence_ref']) <= 500):
            raise ValueError('Not-published resolution requires actual list and saved-draft observation evidence')
        observed = datetime.fromisoformat(check.get('checked_at', ''))
        parsed = urlsplit(check.get('published_list_url', ''))
        match = re.fullmatch(r'/([A-Za-z0-9_-]+)/?', parsed.path)
        if (observed.tzinfo is None or observed < stamps['publish_attempted_at'] or observed > datetime.now(KST)
                or parsed.scheme != 'https' or parsed.netloc not in {'blog.naver.com', 'm.blog.naver.com'}
                or parsed.query or parsed.fragment or not match or (blog_id and match[1] != blog_id)):
            raise ValueError('Not-published resolution must observe the owner after the attempted publication')
        result['publication_check_json'] = json.dumps(check, sort_keys=True, ensure_ascii=False)
    if status == 'PUBLISHED':
        precision = record.get('published_at_precision', 'second')
        if precision not in {'second', 'minute'}:
            raise ValueError('Unknown observed publication timestamp precision')
        result['published_at_precision'] = precision
        url = record.get('published_url', '')
        post_id = record.get('post_id', '')
        if not isinstance(url, str) or not isinstance(post_id, str):
            raise ValueError('Publication URL and post ID must be strings')
        parsed = urlsplit(url)
        match = re.fullmatch(r'/([A-Za-z0-9_-]+)/([1-9][0-9]*)', parsed.path)
        if (parsed.scheme != 'https' or parsed.netloc not in {'blog.naver.com', 'm.blog.naver.com'}
                or parsed.query or parsed.fragment or not match or match[2] != post_id
                or (blog_id and match[1] != blog_id) or 'published_at' not in stamps):
            raise ValueError('Published evidence must identify the observed owner post and publication time')
        result['published_url'] = 'https://blog.naver.com' + parsed.path
        published = stamps['published_at']
        if precision == 'minute' and (published.second or published.microsecond):
            raise ValueError('Minute publication evidence cannot invent seconds')
        attempted = stamps.get('publish_attempted_at', published)
        if ((precision == 'second' and attempted > published)
                or (precision == 'minute' and attempted >= published + timedelta(minutes=1))):
            raise ValueError('Publication cannot precede its attempt')
    elif any(record.get(key) for key in ('published_url', 'post_id', 'published_at')):
        raise ValueError('Only an observed PUBLISHED receipt can assert publication')
    return result


def import_work_receipts(conn: sqlite3.Connection, payload: dict, *, blog_id: str,
                         categories: Iterable[str]) -> dict:
    """Persist a fresh private Work ledger atomically, without generating or clicking."""
    if (payload.get('verified_date') != today_kst().isoformat()
            or not isinstance(payload.get('records'), list) or not blog_id):
        raise ValueError('A freshly verified owner Work ledger is required')
    return merge_work_receipts(conn, payload['records'], blog_id=blog_id, categories=categories)


def merge_work_receipts(conn: sqlite3.Connection, records: list[dict], *, blog_id: str = '',
                        categories: Iterable[str]) -> dict:
    """Merge persisted evidence without claiming a new observation or permitting a rewind."""
    records = [validate_work_receipt(r, blog_id) for r in records]
    if len({r['request_id'] for r in records}) != len(records):
        raise ValueError('Duplicate request identities in Work ledger')
    counts = {}
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        for record in records:
            identity, status = record['request_id'], record['status']
            if record.get('category') not in categories:
                raise ValueError('Unknown Work receipt category')
            old = conn.execute('SELECT * FROM save_receipts WHERE request_id=?', (identity,)).fetchone()
            if old and (old['day'] != record['day'] or old['category'] != record['category']):
                raise ValueError('Conflicting save identity or actual day requires reconciliation')
            if old and old['status'] == 'SAVE_NOT_SAVED' and (
                    status != 'SAVE_NOT_SAVED' or old['save_check_json'] != record.get('save_check_json')):
                raise ValueError('A resolved save attempt cannot be reset or retried')
            if status == 'SAVE_NOT_SAVED' and old and old['status'] not in {
                    'SAVING', 'SAVE_UNCERTAIN', 'SAVE_NOT_SAVED'}:
                raise ValueError('Only an uncertain save can be resolved as not saved')
            if old and old['status'] == 'PUBLISHED' and (status != 'PUBLISHED' or any(
                    old[k] != record.get(k, '') for k in
                    ('published_url', 'post_id', 'published_at', 'published_at_precision'))):
                raise ValueError('Published receipt cannot be downgraded or reassigned')
            if old and old['status'] in PUBLICATION_PENDING and status not in PUBLICATION_PENDING | PUBLICATION_FINISHED:
                raise ValueError('Publication outcome must be reconciled before any retry')
            if old and old['status'] == 'PUBLICATION_NOT_PUBLISHED' and status not in PUBLICATION_FINISHED:
                raise ValueError('A verified failed attempt cannot grant a repeat publication')
            if old and ((old['status'] == 'SAVED_NAVER' and status in {'SAVING', 'SAVE_UNCERTAIN'})
                        or (old['status'] == 'PUBLISH_UNCERTAIN' and status == 'PUBLISHING')):
                raise ValueError('Observed delivery state cannot be rewound')
            if old and old['publish_attempted_at']:
                if record.get('publish_attempted_at', old['publish_attempted_at']) != old['publish_attempted_at']:
                    raise ValueError('Publication attempt identity cannot be reset')
                record['publish_attempted_at'] = old['publish_attempted_at']
                record = validate_work_receipt(record, blog_id)
            for table in ('posts', 'attempts'):
                original = conn.execute(f'SELECT category,status FROM {table} WHERE request_id=?', (identity,)).fetchone()
                if original and original['category'] != record['category']:
                    raise ValueError('Receipt category does not match the original request')
                if status == 'SAVE_NOT_SAVED' and original and original['status'] in {
                        'SAVED_NAVER', *PUBLICATION_PENDING, *PUBLICATION_FINISHED}:
                    raise ValueError('Observed saved or published work cannot be declared absent')
            conn.execute('INSERT INTO save_receipts(request_id,day,category,status,published_url,post_id,'
                         'published_at,published_at_precision,publish_attempted_at,publication_check_json,save_check_json) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?) '
                         'ON CONFLICT(request_id) DO UPDATE SET status=excluded.status,'
                         'published_url=excluded.published_url,post_id=excluded.post_id,'
                         'published_at=excluded.published_at,published_at_precision=excluded.published_at_precision,'
                         'publish_attempted_at=excluded.publish_attempted_at,publication_check_json=excluded.publication_check_json,'
                         'save_check_json=excluded.save_check_json',
                         (identity, record['day'], record['category'], status,
                          *(record.get(k, '') for k in ('published_url', 'post_id', 'published_at')),
                          record.get('published_at_precision', 'second'), record.get('publish_attempted_at', ''),
                          record.get('publication_check_json', old['publication_check_json'] if old else '{}'),
                          record.get('save_check_json', old['save_check_json'] if old else '{}')))
            conn.execute('UPDATE posts SET status=? WHERE request_id=?', (status, identity))
            conn.execute('UPDATE attempts SET status=? WHERE request_id=?', (status, identity))
            conn.execute('INSERT OR IGNORE INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                         (record['day'], record['category'], identity, status))
            counts[status] = counts.get(status, 0) + 1
    return {'status': 'WORK_RECEIPTS_IMPORTED', 'records': len(records), 'states': counts}


def claim_publication(conn: sqlite3.Connection, request_id: str, settings) -> dict:
    """Claim once before Work clicks Publish. Repeating this is never a retry permit."""
    from .inputs import verify_photos
    from .investment import validate_current_post
    from .planning import active_plan, matches_post, saved_identities

    plan = active_plan(settings)
    if not plan or plan.get('reservation'):
        raise ValueError('Automatic publication requires an active unreserved editorial slot')
    ready = json.loads((settings.db_path.parent / 'ready.json').read_text())
    if ready.get('date') != today_kst().isoformat() or ready.get('daily_plan') != plan or ready.get('hold'):
        raise ValueError('Publication requires the current approved handoff')
    items = [item for item in ready.get('posts', []) + ready.get('publication_ready', [])
             if item.get('post', {}).get('request_id') == request_id]
    if len(items) != 1 or items[0].get('requires_fresh_review'):
        raise ValueError('Publication requires one current approved handoff item')
    approved = PostDraft(**items[0]['post'])
    if approved.status != 'APPROVED' or not matches_post(approved, plan):
        raise ValueError('Publication requires the approved post in the active editorial slot')
    packet = json.loads((settings.artifact_dir / f"{approved.as_of_date}-{items[0]['id']:05d}.json").read_text())
    score, decision = review_result(packet.get('review', {}))
    checks = packet.get('review', {}).get('source_checks', [])
    if (packet.get('post') != asdict(approved) or decision != 'PASS'
            or score < settings.config['blog']['review_pass_score'] or score != approved.quality_score
            or not approved.photos
            or (approved.category != 'cooking' and (not isinstance(checks, list) or not checks or any(
                not isinstance(check, dict) or check.get('status') != 'SUPPORTED' for check in checks)))):
        raise ValueError('Publication requires matching approved text and supported review evidence')
    validate_current_post(settings, approved)
    verify_photos(approved.photos)
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT * FROM save_receipts WHERE request_id=?', (request_id,)).fetchone()
        if not row or row['status'] != 'SAVED_NAVER' or row['day'] != today_kst().isoformat():
            raise ValueError('Only a verified saved draft can begin publication once')
        if plan and (plan.get('reservation') or plan['date'] != today_kst().isoformat()
                     or row['category'] != plan['category']):
            raise ValueError('Saved draft does not match the active editorial slot')
        stored = conn.execute('SELECT * FROM posts WHERE request_id=?', (request_id,)).fetchall()
        if len(stored) != 1 or stored[0]['status'] != 'SAVED_NAVER':
            raise ValueError('A saved receipt alone cannot authorize an unapproved publication')
        post = load_post(stored[0])
        post.status = 'APPROVED'
        if asdict(post) != asdict(approved):
            raise ValueError('Saved draft differs from the approved handoff')
        if conn.execute("SELECT 1 FROM save_receipts WHERE status IN "
                        "('SAVING','SAVE_UNCERTAIN','PUBLISHING','PUBLISH_UNCERTAIN')").fetchone():
            raise ValueError('Reconcile the uncertain publication before another attempt')
        if saved_identities(conn, today_kst().isoformat()) - {request_id}:
            raise ValueError('Another delivery already occupies the daily slot')
        stamp = datetime.now(KST).isoformat()
        conn.execute("UPDATE save_receipts SET status='PUBLISHING',publish_attempted_at=? WHERE request_id=?",
                     (stamp, request_id))
        conn.execute("UPDATE posts SET status='PUBLISHING' WHERE request_id=?", (request_id,))
        conn.execute("UPDATE attempts SET status='PUBLISHING' WHERE request_id=?", (request_id,))
    return {'status': 'PUBLICATION_CLAIMED', 'request_id': request_id, 'publish_attempted_at': stamp}


def recent_titles(conn: sqlite3.Connection, limit: int = 100) -> list[str]:
    rows = conn.execute("SELECT title FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [r[0] for r in rows]


def text_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[가-힣A-Za-z0-9]+", text.lower()) if len(t) >= 2}


def jaccard_similarity(a: str, b: str) -> float:
    aa, bb = text_tokens(a), text_tokens(b)
    if not aa or not bb:
        return 0.0
    return len(aa & bb) / len(aa | bb)


def max_title_similarity(title: str, existing: Iterable[str]) -> float:
    return max((jaccard_similarity(title, x) for x in existing), default=0.0)


def save_post(conn: sqlite3.Connection, post: PostDraft) -> int:
    cur = conn.execute(
        """
        INSERT INTO posts (
          category, subcategory, topic, title, body, tags_json, sources_json,
          as_of_date, quality_score, status, fingerprint, created_at,
          request_id, photos_json, provenance_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            post.category,
            post.subcategory,
            post.topic,
            post.title,
            post.body,
            json.dumps(post.tags, ensure_ascii=False),
            json.dumps(post.source_urls, ensure_ascii=False),
            post.as_of_date,
            post.quality_score,
            post.status,
            post.fingerprint,
            datetime.now(UTC).isoformat(),
            post.request_id,
            json.dumps(post.photos, ensure_ascii=False),
            json.dumps(post.provenance, ensure_ascii=False),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_saved(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute(
        "UPDATE posts SET status='SAVED_NAVER', draft_saved_at=? WHERE id=?",
        (datetime.now(UTC).isoformat(), post_id),
    )
    conn.commit()


def choose_categories(config: dict, count: int, used: dict | None = None) -> list[str]:
    categories = config["categories"]
    keys = list(categories)
    offset = today_kst().toordinal() % len(keys)
    keys = keys[offset:] + keys[:offset]
    per_cat = {k: (used or {}).get(k, 0) for k in keys}
    result: list[str] = []
    while len(result) < count:
        available = [k for k in keys if per_cat[k] < int(categories[k]["max_daily"])]
        if not available:
            break
        key = min(available, key=lambda k: per_cat[k] / max(1, categories[k].get("weight", 1)))
        result.append(key)
        per_cat[key] += 1
    return result


def reserve_attempt(conn: sqlite3.Connection, config: dict, daily_target: int, candidates: list):
    """Consume a real source once; empty categories never cause invented topics."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM save_receipts WHERE status IN ('PUBLISHING','PUBLISH_UNCERTAIN')").fetchone():
            return None
        rows = conn.execute(
            "SELECT category, status, COUNT(*) FROM attempts WHERE day=? GROUP BY category, status",
            (today_kst().isoformat(),),
        ).fetchall()
        plan = config.get('daily_plan')
        if plan:
            if plan.get('reservation'):
                return None
            if plan['date'] != today_kst().isoformat():
                raise ValueError('Reload settings after the KST date changes')
            from .planning import matches_post, saved_count
            if saved_count(conn, plan) or conn.execute(
                "SELECT 1 FROM posts WHERE status IN ('SAVING','SAVE_UNCERTAIN') LIMIT 1"
            ).fetchone():
                return None
            if any(matches_post(load_post(row), plan) for row in conn.execute(
                "SELECT * FROM posts WHERE status IN ('APPROVED','TEXT_APPROVED','IMAGES_PENDING')"
            )):
                return None
            daily_target = min(daily_target, plan['target'])
        attempted = sum(row[2] for row in rows)
        dropped = sum(row[2] for row in rows if row[1] == "DROP_REVIEW")
        # Permit one replacement after editorial rejection, with a hard cap on paid attempts.
        if attempted >= daily_target + min(dropped, 1) or attempted - dropped >= daily_target:
            return None
        used = {category: sum(n for c, status, n in rows
                              if c == category and status != "DROP_REVIEW")
                for category, _, _ in rows}
        # A category with a rejected draft can be attempted once more, never indefinitely.
        exhausted = {category for category, _, _ in rows
                     if sum(n for c, _, n in rows if c == category) >=
                     int(config["categories"][category]["max_daily"]) +
                     int(any(c == category and status == "DROP_REVIEW" for c, status, _ in rows))}
        seen = {row[0] for row in conn.execute("SELECT request_id FROM attempts")}
        events = {row[0] for row in conn.execute("SELECT event_key FROM attempts WHERE event_key IS NOT NULL")}
        available = [r for r in candidates if r.id not in seen
                     and r.provenance.get("policy_event_key") not in events]
        if plan:
            from .planning import matches_request
            available = [r for r in available if matches_request(r, plan)]
        present = {r.category for r in available}
        limited = {"categories": {k: v for k, v in config["categories"].items()
                                  if k in present and k not in exhausted}}
        if not limited["categories"]:
            return None
        choices = choose_categories(limited, 1, used)
        if not choices:
            return None
        category = choices[0]
        request = next(r for r in available if r.category == category)
        cur = conn.execute(
            "INSERT INTO attempts(day, category, request_id, plan_json, event_key) VALUES (?, ?, ?, ?, ?)",
            (today_kst().isoformat(), category, request.id, json.dumps(plan or {}, sort_keys=True),
             request.provenance.get("policy_event_key")),
        )
        return int(cur.lastrowid), request


def set_status(conn: sqlite3.Connection, post_id: int, status: str) -> None:
    with conn:
        conn.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))


def load_post(row: sqlite3.Row) -> PostDraft:
    return PostDraft(
        category=row["category"], subcategory=row["subcategory"], topic=row["topic"],
        title=row["title"], body=row["body"], tags=json.loads(row["tags_json"]),
        source_urls=json.loads(row["sources_json"]), as_of_date=row["as_of_date"],
        quality_score=row["quality_score"], status=row["status"],
        request_id=row["request_id"], photos=json.loads(row["photos_json"]),
        provenance=json.loads(row["provenance_json"]),
    )


def validate_post(post: PostDraft, category_info: dict) -> None:
    if post.category == 'origins':
        from .origins import validate_origin_post
        validate_origin_post(post)
    if not post.title or not post.body or post.subcategory not in category_info["subcategories"]:
        raise ValueError("Empty draft or invalid subcategory")
    if date.fromisoformat(post.as_of_date) != today_kst():
        raise ValueError("Draft must specify today's KST reference date")
    if post.category == "cooking" and not post.photos:
        raise ValueError("Cooking without owner photos cannot be approved")
    if post.category != "cooking" and not post.source_urls:
        raise ValueError("No search-backed sources; hold draft")
    for url in post.source_urls:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("Invalid source URL")
    if re.search(r"수익\s*보장|원금\s*보장|무조건\s*(상승|매수|매도)|확정\s*수익", post.body):
        raise ValueError("Guaranteed-return language requires manual review")


def review_result(review: dict) -> tuple[int, str]:
    scores = review.get("scores", [])
    values = scores
    if not isinstance(values, list) or len(values) != 6:
        return 0, "DROP"
    if any(type(v) is not int or not 1 <= v <= 5 for v in values):
        return 0, "DROP"
    total = sum(values)
    if type(review.get("total")) is not int or total != review["total"]:
        return 0, "DROP"
    decision = str(review.get("decision", "DROP")).upper()
    if decision not in {"PASS", "REWRITE", "DROP"}:
        return total, "DROP"
    # A contradictory PASS must not silently ignore requested corrections.
    instructions = str(review.get("rewrite_instructions", "")).strip()
    if decision == "PASS" and review.get("issues") and instructions not in {
        "", "없음", "수정 없음", "수정 필요 없음", "none", "None",
    }:
        decision = "REWRITE"
    # High total cannot override an accuracy or category-safety failure.
    if values[0] < 4 or values[5] < 4 or review.get("blocking_issues"):
        decision = "REWRITE" if total >= 20 else "DROP"
    return total, decision


def render_post_text(post: PostDraft) -> str:
    sources = "\n".join(f"- {u}" for u in post.source_urls[:8])
    text = post.body.rstrip()
    if sources:
        text += f"\n\n참고자료\n{sources}"
    if post.provenance.get("source_url"):
        text += f"\n\n커뮤니티 봇 원본 자료\n{post.provenance['source_url']}"
    captions = [f"{i + 1}. {p['caption']}" for i, p in enumerate(post.photos) if p.get("caption")]
    if captions:
        text += "\n\n제공 사진 설명\n" + "\n".join(captions)
    if post.tags:
        text += "\n\n" + " ".join(f"#{t}" for t in post.tags)
    return text
