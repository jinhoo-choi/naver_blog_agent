"""Private user input queue and read-only community-bot source adapter."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .config import Settings
from .core import today_kst


@dataclass
class ContentRequest:
    id: str
    category: str
    data: dict
    photos: list[dict] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    def prompt_data(self) -> dict:
        # Local paths and employee assignment fields never go to the model.
        return {
            "category": self.category,
            "data": self.data,
            "photos": [{"number": i + 1, "caption": p.get("caption", "")}
                       for i, p in enumerate(self.photos)],
            "provenance": self.provenance,
        }


def verify_photos(photos: list[dict]) -> None:
    if not photos:
        raise ValueError("Cooking requires at least one supplied photo")
    for photo in photos:
        path = Path(photo["file"])
        if not path.is_file() or not 0 < path.stat().st_size <= 20_000_000:
            raise ValueError("Photo missing, empty or larger than 20 MB")
        if path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ValueError("Unsupported photo format")
        if hashlib.sha256(path.read_bytes()).hexdigest() != photo["sha256"]:
            raise ValueError("Photo changed after request was registered")


def enqueue_file(settings: Settings, source: Path) -> str:
    """Validate first, copy owned photos, then publish an immutable queue manifest."""
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    if raw.get("enabled", True) is not True:
        raise ValueError("Enable this request after replacing example values")
    request_id = raw.get("id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", request_id):
        raise ValueError("Request id must be a unique ASCII slug")
    category = raw.get("category")
    if category in {"parenting", "exercise"}:
        question = raw.get("question", "")
        if not isinstance(question, str) or len(question.strip()) < 3:
            raise ValueError("Parenting requires the owner's actual question")
        age = raw.get("age_months")
        if age is not None and (type(age) is not int or not 0 <= age <= 216):
            raise ValueError("age_months must be supplied accurately or omitted")
        context = raw.get("context", "")
        if not isinstance(context, str):
            raise TypeError("context must be text")
        data = {"question": question.strip(), "age_months": age, "context": context,
                "benchmark_query": str(raw.get("benchmark_query", ""))}
        photos = []
    elif category == "cooking":
        recipe = raw.get("recipe", {})
        if not isinstance(recipe, dict) or not isinstance(recipe.get("name"), str):
            raise ValueError("Cooking requires a recipe name")
        if not recipe["name"].strip():
            raise ValueError("Recipe name is empty")
        for key in ("ingredients", "steps"):
            values = recipe.get(key)
            if not isinstance(values, list) or not values or any(
                not isinstance(v, str) or not v.strip() for v in values
            ):
                raise ValueError("Cooking requires supplied ingredients and steps")
        data = {key: recipe[key] for key in (
            "name", "ingredients", "steps", "servings", "total_time", "tips",
        ) if key in recipe}
        raw_photos = raw.get("photos", [])
        if not isinstance(raw_photos, list) or not 1 <= len(raw_photos) <= 15:
            raise ValueError("Cooking requires 1 to 15 supplied photos")
        photos = []
        for item in raw_photos:
            path = (source.parent / item["file"]).resolve()
            if not path.is_file() or not 0 < path.stat().st_size <= 20_000_000:
                raise ValueError("Photo missing, empty or larger than 20 MB")
            photos.append({"file": str(path), "caption": str(item.get("caption", "")),
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        verify_photos(photos)
    else:
        raise ValueError("Only parenting questions or supplied cooking material can be enqueued")

    settings.inbox_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.inbox_dir / request_id
    destination.mkdir()  # An existing id is never silently overwritten.
    for i, photo in enumerate(photos):
        original = Path(photo["file"])
        target = destination / f"photo-{i + 1:02d}{original.suffix.lower()}"
        shutil.copyfile(original, target)
        photo["file"] = str(target.resolve())
    request = ContentRequest(request_id, category, data, photos, {"source": "owner_input"})
    manifest = destination / "request.json"
    manifest.write_text(json.dumps(asdict(request), ensure_ascii=False, indent=2), encoding="utf-8")
    return request_id


def _get_json(url: str):
    request = Request(url, headers={"Accept": "application/vnd.github+json",
                                    "User-Agent": "naver-blog-agent/0.3"})
    with urlopen(request, timeout=25) as response:
        payload = response.read(5_000_001)
    if len(payload) > 5_000_000:
        raise ValueError("Community source exceeded size limit")
    return json.loads(payload)


def fetch_community(config: dict) -> tuple[list, dict]:
    repo = config["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid community repository")
    path = "data/posts_latest.json"
    base = f"https://api.github.com/repos/{repo}"
    commits = _get_json(f"{base}/commits?path={quote(path, safe='')}&per_page=1&sha=main")
    if not commits:
        return [], {}
    commit = commits[0]
    sha = commit["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Invalid source revision")
    content = _get_json(f"{base}/contents/{path}?ref={sha}")
    if content.get("encoding") != "base64":
        raise ValueError("Unexpected community export encoding")
    records = json.loads(base64.b64decode(content["content"]))
    if not isinstance(records, list):
        raise TypeError("Community export must be a list")
    stamp = datetime.fromisoformat(commit["commit"]["committer"]["date"])
    stamp = stamp.astimezone(ZoneInfo("Asia/Seoul"))
    provenance = {
        "repository": repo, "commit": sha,
        "snapshot_date": stamp.date().isoformat(),
        "snapshot_at": stamp.isoformat(),
        "url": f"https://github.com/{repo}/blob/{sha}/{path}",
    }
    return records, provenance


def community_request(record: dict, provenance: dict, config: dict) -> ContentRequest | None:
    # posts_latest is the upstream final distribution export. Blog standards are separate.
    allowed_kinds = set(config.get("allowed_kinds", ["research", "policy", "disclosure"]))
    if record.get("kind") not in allowed_kinds:
        return None
    score = record.get("score")
    if not isinstance(score, dict) or score.get("fatal") != []:
        return None
    values = [score.get(k) for k in ("factual", "useful", "natural", "compliant", "gain", "fit")]
    if any(type(v) is not int or not 1 <= v <= 5 for v in values):
        return None
    if values[0] < 4 or values[3] < 4 or values[5] < 3:
        return None
    if round(sum(values) / 30 * 20, 1) < config.get("min_score", 14):
        return None
    if not all(isinstance(record.get(k), str) and record[k].strip()
               for k in ("id", "facts", "src", "body")):
        return None
    # Policy exports may carry a stock merely as an upstream posting destination.
    sector_only = record.get("kind") == "policy" and (
        record.get("theme_assigned") is True or record.get("board_mapping") == "SECTOR_PROXY"
    )
    if not sector_only and config.get("require_clear_issue_for_stocks", True) and (
        record.get("stock_name") or record.get("stock_code")
    ):
        # Generated prose and caution/instruction lines are not event evidence.
        issue_text = " ".join(line for line in record["facts"].splitlines()
                              if not line.lstrip().startswith("※"))
        if not re.search(
            r"(계약|수주|기술이전|기술수출|임상|승인|허가|규제|정책|실적|가이던스|"
            r"자사주|배당|합병|분할|유증|무증|M&A|인수|매각|상장|특허|소송|"
            r"매출|영업이익|순이익|증설|생산능력)",
            issue_text,
            re.IGNORECASE,
        ):
            return None
    source = urlsplit(record["src"])
    if source.scheme != "https" or not source.hostname:
        return None
    stamp = date.fromisoformat(provenance["snapshot_date"])
    # A fresh export or a future event date cannot make an old report current.
    dates = re.findall(
        r"(?:발간일|발행일|공시일|접수일|보도\s*시각|보도일|자료\s*기준일|기준일)"
        r"\s*[:：]?\s*(20\d{2}-\d{2}-\d{2})", record["facts"])
    if not dates:
        return None
    data_date = min(date.fromisoformat(d) for d in dates)
    age_limit = int(config.get("max_age_days", 1))
    if not (0 <= (today_kst() - stamp).days <= age_limit
            and 0 <= (today_kst() - data_date).days <= age_limit):
        return None
    # Strip assignee, private diagnostics and raw model tails.
    data = {key: record.get(key, "") for key in (
        "id", "kind", "stock_code", "stock_name", "title", "facts", "src", "body",
    )}
    data["source_date"] = data_date.isoformat()
    if sector_only:
        data.update(stock_name="", stock_code="", sector_only=True)
    key = hashlib.sha256(f"{provenance['repository']}:{record['id']}".encode()).hexdigest()[:24]
    return ContentRequest(f"community-{key}", "investment", data, provenance={
        **provenance, "source_date": data_date.isoformat(), "source_url": record["src"],
    })


def collect_requests(settings: Settings) -> tuple[list[ContentRequest], list[dict]]:
    requests, notices = [], []
    for path in sorted(settings.inbox_dir.glob("*/request.json")):
        try:
            request = ContentRequest(**json.loads(path.read_text(encoding="utf-8")))
            if request.category == "cooking":
                if not all(request.data.get(k) for k in ("name", "ingredients", "steps")):
                    raise ValueError("Recipe incomplete")
                verify_photos(request.photos)
            elif request.category not in {"parenting", "exercise"} or not request.data.get("question"):
                raise ValueError("Question missing or unsupported category")
            requests.append(request)
        except (OSError, ValueError, TypeError, KeyError):
            notices.append({"status": "INPUT_REJECTED"})
    context_path = settings.db_path.parent / "context.json"
    context = json.loads(context_path.read_text()) if context_path.exists() else {}
    config = settings.config.get("community", {})
    if config.get("enabled", False):
        try:
            records, provenance = fetch_community(config)
            if config.get("require_today_snapshot", True):
                stamp = datetime.fromisoformat(provenance["snapshot_at"])
                stamp = stamp.astimezone(ZoneInfo("Asia/Seoul"))
                if stamp.date() != today_kst() or stamp.hour < 8:
                    notices.append({"status": "COMMUNITY_SOURCE_PENDING"})
                    return requests, notices
            eligible = 0
            for record in records:
                if today_kst().isoformat() <= context.get("exclude_investment_topics_until", ""):
                    source_text = " ".join(str(record.get(k, "")) for k in ["stock_name", "title", "facts"])
                    if any(topic in source_text for topic in context.get("excluded_investment_topics", [])):
                        continue
                try:
                    request = community_request(record, provenance, config)
                    if request:
                        requests.append(request)
                        eligible += 1
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue  # Malformed one-off records do not block the owner's input.
            if not eligible:
                notices.append({"status": "NO_ELIGIBLE_INVESTMENT"})
        except (OSError, ValueError, TypeError, KeyError):
            notices.append({"status": "COMMUNITY_SOURCE_UNAVAILABLE"})
    return requests, notices
