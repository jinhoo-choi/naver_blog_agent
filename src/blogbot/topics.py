"""Optional API HUB trend ranking of existing, eligible inputs; no new topics."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import replace
from datetime import date, timedelta
from http.client import HTTPException
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .core import today_kst
from .images import atomic_json

ENDPOINT = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
CATEGORIES = ("parenting", "exercise", "investment")
VERSION = "naver-momentum-v1"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward API credentials to another endpoint.


def _keyword(request) -> str:
    # Explicitly public keyword only: never send the private question/context.
    value = request.data.get("benchmark_query")
    if not value and request.category == "investment":
        value = request.data.get("stock_name")
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return value if re.fullmatch(r"[가-힣A-Za-z0-9 ()·&-]{2,60}", value) else ""


def _cache_key(keyword: str) -> str:
    return hashlib.sha256(f"{VERSION}:{keyword}".encode()).hexdigest()


def _signal(points, end: date) -> dict:
    if not isinstance(points, list):
        raise ValueError("Invalid trend series")
    values = {}
    start = end - timedelta(days=27)
    for point in points:
        stamp = date.fromisoformat(point["period"])
        value = point["ratio"]
        if (stamp in values or not start <= stamp <= end or
                type(value) not in (int, float) or not math.isfinite(value) or
                not 0 <= value <= 100):
            raise ValueError("Invalid trend observation")
        values[stamp] = value
    days = [end - timedelta(days=n) for n in range(13, -1, -1)]
    # Missing/low-volume data is not zero demand and cannot drive ranking.
    if any(day not in values for day in days):
        return {"status": "INSUFFICIENT_DATA", "window_end": end.isoformat()}
    previous, recent = ([values[d] for d in days[:7]], [values[d] for d in days[7:]])
    if min(sum(v > 0 for v in previous), sum(v > 0 for v in recent)) < 4:
        return {"status": "INSUFFICIENT_DATA", "window_end": end.isoformat()}
    growth = sum(recent) / sum(previous) - 1
    if not math.isfinite(growth):
        raise ValueError("Invalid trend growth")
    return {"status": "OK", "window_end": end.isoformat(),
            "growth_7d": round(growth, 4),
            "priority": round(max(-1.0, min(2.0, growth)), 4)}


def _fetch(keywords: list[str], end: date, client_id: str, secret: str) -> dict:
    payload = {"startDate": (end - timedelta(days=27)).isoformat(),
               "endDate": end.isoformat(), "timeUnit": "date",
               "keywordGroups": [{"groupName": str(i), "keywords": [word]}
                                 for i, word in enumerate(keywords)]}
    request = Request(ENDPOINT, data=json.dumps(payload).encode(), headers={
        "X-NCP-APIGW-API-KEY-ID": client_id, "X-NCP-APIGW-API-KEY": secret,
        "Content-Type": "application/json", "User-Agent": "naver-blog-agent/0.5",
    }, method="POST")
    with build_opener(_NoRedirect()).open(request, timeout=8) as response:
        raw = response.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("Trend response too large")
    data = json.loads(raw)
    if data.get("endDate") != end.isoformat():
        raise ValueError("Unexpected trend window")
    result = {}
    for item in data["results"]:
        index = int(item["title"])
        if not 0 <= index < len(keywords) or keywords[index] in result:
            raise ValueError("Unexpected trend group")
        keyword = keywords[index]
        if item.get("keywords") != [keyword]:
            raise ValueError("Unexpected trend keyword")
        result[keyword] = _signal(item["data"], end)
    if len(result) != len(keywords):
        raise ValueError("Incomplete trend response")
    return result


def rank_candidates(settings, conn, candidates: list, daily_target: int) -> list:
    """Keep quotas/order slots intact; only reorder fully measured shortlists."""
    if not settings.config.get("topics", {}).get("enabled", False):
        return candidates
    try:
        return _rank(settings, conn, candidates, daily_target)
    except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError, OverflowError):
        # Optional research must not prevent the existing generation pipeline.
        return candidates


def _rank(settings, conn, candidates: list, daily_target: int) -> list:
    today = today_kst()
    end = today - timedelta(days=1)
    used = {row[0] for row in conn.execute("SELECT request_id FROM attempts")}
    daily_used = dict(conn.execute(
        "SELECT category,COUNT(*) FROM attempts WHERE day=? GROUP BY category",
        (today.isoformat(),),
    ).fetchall())
    if sum(daily_used.values()) >= daily_target:
        return candidates
    shortlists = {}
    for category in CATEGORIES:
        quota = settings.config["categories"].get(category, {}).get("max_daily", 0)
        if daily_used.get(category, 0) < quota:
            indices = [i for i, r in enumerate(candidates)
                       if r.category == category and r.id not in used][:5]
            if len(indices) > 1:
                shortlists[category] = indices
    if not shortlists:
        return candidates
    directory = settings.db_path.parent
    cache_path = directory / "topic-cache.json"
    report_path = directory / "topic-selection.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if cache.get("version") != VERSION or not isinstance(cache.get("entries"), dict):
            cache = {}
    except (OSError, ValueError, TypeError, AttributeError):
        cache = {}
    entries = cache.get("entries", {})
    client_id = os.getenv("NAVER_API_HUB_CLIENT_ID", "")
    secret = os.getenv("NAVER_API_HUB_CLIENT_SECRET", "")
    connected = bool(client_id and secret)
    output, reports, calls, circuit_open = list(candidates), [], 0, False
    for category, indices in shortlists.items():
        words = list(dict.fromkeys(_keyword(candidates[i]) for i in indices))
        words = [word for word in words if word]
        missing = [word for word in words
                   if entries.get(_cache_key(word), {}).get("window_end") != end.isoformat()]
        error = None
        if missing and connected and not circuit_open:
            try:
                calls += 1  # At most one call/category, no automatic retry.
                fresh = _fetch(missing, end, client_id, secret)
                entries.update({_cache_key(word): signal for word, signal in fresh.items()})
            except (OSError, HTTPException, ValueError, TypeError, KeyError,
                    AttributeError, OverflowError) as exc:
                error = type(exc).__name__  # Never log headers, response bodies or credentials.
                circuit_open = True
        measured = []
        for i in indices:
            word = _keyword(candidates[i])
            signal = entries.get(_cache_key(word), {}) if word else {}
            age = (end - date.fromisoformat(signal["window_end"])).days if signal else None
            usable = (signal.get("status") == "OK" and age is not None and 0 <= age <= 7
                      and type(signal.get("priority")) in (int, float)
                      and math.isfinite(signal["priority"]))
            measured.append({"index": i, "id": candidates[i].id, "keyword": word,
                             "usable": usable, "signal": signal if usable else {},
                             "status": ("FRESH" if age == 0 else "STALE_CACHE") if usable else
                                       ("EXPIRED_CACHE" if age is not None and not 0 <= age <= 7
                                        else signal.get("status", "UNAVAILABLE"))})
        # Do not compare mismatched windows or promote measured over unmeasured inputs.
        comparable = (all(row["usable"] for row in measured) and
                      len({row["signal"]["window_end"] for row in measured}) == 1)
        ordered = sorted(measured, key=lambda row: -row["signal"]["priority"]) if comparable else measured
        for slot, row in zip(indices, ordered):
            request = candidates[row["index"]]
            if comparable:
                request = replace(request, provenance={**request.provenance, "topic_selection": {
                    "version": VERSION, "provider": "naver_api_hub", "keyword": row["keyword"],
                    **row["signal"], "cache_status": row["status"],
                    "basis": "relative_7d_growth_not_volume_competition_or_rank_prediction",
                }})
            output[slot] = request
        reports.append({"category": category, "ranked": comparable, "error": error,
                        "candidates": measured, "order": [row["id"] for row in ordered]})
    # Bound stored cache, retain only windows still eligible for fallback.
    entries = {key: value for key, value in entries.items()
               if 0 <= (end - date.fromisoformat(value["window_end"])).days <= 7}
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(cache_path, {"version": VERSION, "entries": entries})
    atomic_json(report_path, {"date": today.isoformat(), "version": VERSION,
                             "credentials_present": connected, "api_calls": calls,
                             "categories": reports})
    return output
