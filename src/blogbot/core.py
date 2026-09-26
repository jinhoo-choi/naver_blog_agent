from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

KST = timezone(timedelta(hours=9))


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
    return conn


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
          as_of_date, quality_score, status, fingerprint, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_saved(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute(
        "UPDATE posts SET status='SAVED_NAVER', draft_saved_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), post_id),
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


def reserve_attempt(conn: sqlite3.Connection, config: dict, daily_target: int):
    """Reserve before a paid call; retries and overlapping dispatches share a KST budget."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM attempts WHERE day=? GROUP BY category",
            (today_kst().isoformat(),),
        ).fetchall()
        used = {row[0]: row[1] for row in rows}
        if sum(used.values()) >= daily_target:
            return None
        choices = choose_categories(config, 1, used)
        if not choices:
            return None
        category = choices[0]
        cur = conn.execute(
            "INSERT INTO attempts(day, category) VALUES (?, ?)",
            (today_kst().isoformat(), category),
        )
        return int(cur.lastrowid), category


def set_status(conn: sqlite3.Connection, post_id: int, status: str) -> None:
    with conn:
        conn.execute("UPDATE posts SET status=? WHERE id=?", (status, post_id))


def load_post(row: sqlite3.Row) -> PostDraft:
    return PostDraft(
        category=row["category"], subcategory=row["subcategory"], topic=row["topic"],
        title=row["title"], body=row["body"], tags=json.loads(row["tags_json"]),
        source_urls=json.loads(row["sources_json"]), as_of_date=row["as_of_date"],
        quality_score=row["quality_score"], status=row["status"],
    )


def validate_post(post: PostDraft, category_info: dict) -> None:
    if not post.title or not post.body or post.subcategory not in category_info["subcategories"]:
        raise ValueError("Empty draft or invalid subcategory")
    if date.fromisoformat(post.as_of_date) != today_kst():
        raise ValueError("Draft must specify today's KST reference date")
    if not post.source_urls:
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
    # High total cannot override an accuracy or category-safety failure.
    if values[0] < 4 or values[5] < 4 or review.get("blocking_issues"):
        decision = "REWRITE" if total >= 20 else "DROP"
    return total, decision


def render_post_text(post: PostDraft) -> str:
    sources = "\n".join(f"- {u}" for u in post.source_urls[:8])
    text = f"기준일: {post.as_of_date}\n\n{post.body.rstrip()}"
    if sources:
        text += f"\n\n참고자료\n{sources}"
    if post.tags:
        text += "\n\n" + " ".join(f"#{t}" for t in post.tags)
    return text
