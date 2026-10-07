"""Private user input queue and read-only community-bot source adapter."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from .config import Settings
from .core import today_kst
from .editorial import content_style
from .planning import active_plan, editorial_type, matches_request

MAX_MANAGED_PHOTO_BYTES = 12_000_000  # Encrypted transport headroom, not an image-count rule.


def managed_queue_photos(settings: Settings) -> list[Path]:
    paths = []
    for path in settings.inbox_dir.glob('*/photo-*'):
        if re.fullmatch(r'photo-\d+\.(?:jpg|jpeg|png|webp)', path.name):
            if (path.is_symlink() or not path.resolve().is_relative_to(settings.inbox_dir.resolve())
                    or not path.is_file() or not 0 < path.stat().st_size <= 20_000_000):
                raise ValueError('Invalid managed queue photo')
            paths.append(path)
    return paths


def verify_review_transport_budget(settings: Settings, additional_bytes: int = 0) -> None:
    if sum(p.stat().st_size for p in managed_queue_photos(settings)) + additional_bytes > MAX_MANAGED_PHOTO_BYTES:
        raise ValueError('Review photo transport capacity exceeded; preserve existing files')


@dataclass
class ContentRequest:
    id: str
    category: str
    data: dict
    photos: list[dict] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category == 'investment' and self.data.get('investment_mode') == 'life-economics-v1':
            from .life_economics import VERSION, validate_data
            bundle = validate_data(self.data, fresh=False)
            if (self.provenance.get('investment_mode', VERSION) != VERSION
                    or self.provenance.get('life_economics', bundle) != bundle):
                raise ValueError('Conflicting life-economics provenance')
            self.provenance = {**self.provenance, 'investment_mode': VERSION,
                               'life_economics': bundle}
        if self.category == 'origins':
            from .origins import validate_origin_data
            self.provenance = {**self.provenance, 'origins': validate_origin_data(self.data)}
        if content_style(self.category, self.data) == 'review':
            # Persist the style with the text-approved DB row, before media completes.
            # Downstream ready export and direct-writer preflight read post provenance.
            self.provenance = {**self.provenance, 'content_style': 'review'}

    def prompt_data(self) -> dict:
        # Local paths and employee assignment fields never go to the model.
        return {
            "category": self.category,
            "data": self.data,
            "photos": [{"number": i + 1, "caption": p.get("caption", ""),
                        **({k: p[k] for k in ('origin', 'role', 'section_heading', 'source_url')
                            if k in p} if content_style(self.category, self.data) == 'review' else {})}
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


def origin_photo_metadata(item: dict) -> dict:
    if not isinstance(item, dict) or item.get('approved') is not True or item.get('role') != 'thumbnail':
        raise ValueError('Origins requires an explicitly approved supplied thumbnail')
    return {'approved': True, 'role': 'thumbnail', 'generated': bool(item.get('generated', False))}


def review_photo_metadata(item: dict) -> dict:
    """Allow only declared layout/source metadata, never purchase identifiers."""
    if not isinstance(item, dict) or item.get('generated'):
        raise ValueError('Review evidence must be a supplied actual photo')
    origin = item.get('origin', 'owner')
    if origin not in ('owner', 'seller') or item.get('role') not in (None, 'hero'):
        raise ValueError('Unsupported review photo metadata')
    if origin == 'seller' and item.get('role') == 'hero':
        raise ValueError('Review hero must be an owner photo')
    result = {'origin': origin}
    if item.get('role'):
        result['role'] = item['role']
    if 'section_heading' in item:
        heading = item['section_heading']
        if not isinstance(heading, str) or not 1 <= len(heading.strip()) <= 120 or '\n' in heading:
            raise ValueError('Invalid review photo section heading')
        result['section_heading'] = heading.strip()
    if origin == 'seller':
        source = item.get('source_url')
        if not isinstance(source, str):
            raise ValueError('Seller screenshot needs its source URL')
        parsed = urlsplit(source)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or len(source) > 2000):
            raise ValueError('Invalid seller screenshot source URL')
        result['source_url'] = source
    return result


def enqueue_file(settings: Settings, source: Path) -> str:
    """Validate first, copy owned photos, then publish an immutable queue manifest."""
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    if raw.get("enabled", True) is not True:
        raise ValueError("Enable this request after replacing example values")
    request_id = raw.get("id", "")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", request_id):
        raise ValueError("Request id must be a unique ASCII slug")
    category = raw.get("category")
    style = content_style(category, raw)
    if category in {"parenting", "exercise", "origins"}:
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
        if 'editorial_type' in raw:
            data['editorial_type'] = editorial_type(category, raw)
        photos = []
    elif category == 'investment':
        from .life_economics import validate_data
        data = {**validate_data(raw), 'context': str(raw.get('context', '')),
                'benchmark_query': str(raw.get('benchmark_query', ''))}
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

    if category == 'origins':
        from .origins import validate_origin_data
        data.update({k: raw[k] for k in ('purchase', 'affiliate') if k in raw})
        validate_origin_data(data)
        raw_photos = raw.get('photos', [])
        if not isinstance(raw_photos, list) or len(raw_photos) != 1:
            raise ValueError('Origins requires one approved supplied thumbnail before paid preparation')
        photos = []
        for item in raw_photos:
            metadata = origin_photo_metadata(item)
            path = (source.parent / item['file']).resolve()
            if not path.is_file() or not 0 < path.stat().st_size <= 20_000_000:
                raise ValueError('Thumbnail missing, empty or larger than 20 MB')
            photos.append({'file': str(path), 'caption': str(item.get('caption', '')),
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), **metadata})
        if photos:
            verify_photos(photos)
            verify_review_transport_budget(settings, sum(Path(p['file']).stat().st_size for p in photos))

    if 'content_style' in raw:
        data['content_style'] = style
    if style == 'review':
        raw_photos = raw.get('photos', [])
        if not isinstance(raw_photos, list) or not raw_photos:
            raise ValueError('Review requires supplied photos')
        photos = []
        for item in raw_photos:
            metadata = review_photo_metadata(item)
            path = (source.parent / item['file']).resolve()
            if not path.is_file() or not 0 < path.stat().st_size <= 20_000_000:
                raise ValueError('Photo missing, empty or larger than 20 MB')
            photos.append({'file': str(path), 'caption': str(item.get('caption', '')),
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                           **metadata})
        verify_photos(photos)
        if not any(p['origin'] == 'owner' for p in photos):
            raise ValueError('Review requires an actual owner photo')
        verify_review_transport_budget(settings, sum(Path(p['file']).stat().st_size for p in photos))

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


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_json(url: str):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "naver-blog-agent/0.3"}
    token = os.environ.get('GITHUB_TOKEN', '')
    if token and urlsplit(url).hostname == 'api.github.com':
        headers['Authorization'] = f'Bearer {token}'
    request = Request(url, headers=headers)
    with build_opener(_NoRedirect()).open(request, timeout=25) as response:
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
    return _community_snapshot(repo, commit)


def _community_snapshot(repo, commit):
    sha = commit['sha']
    if not re.fullmatch(r'[a-f0-9]{40}', sha):
        raise ValueError('Invalid source revision')
    path = 'data/posts_latest.json'
    content = _get_json(f'https://api.github.com/repos/{repo}/contents/{path}?ref={sha}')
    if content.get('encoding') != 'base64':
        raise ValueError('Unexpected community export encoding')
    records = json.loads(base64.b64decode(content['content'], validate=False))
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise TypeError('Community export must be a list of records')
    stamp = datetime.fromisoformat(commit['commit']['committer']['date'])
    if stamp.tzinfo is None:
        raise ValueError('Community snapshot timestamp needs timezone')
    stamp = stamp.astimezone(ZoneInfo('Asia/Seoul'))
    return records, {'repository': repo, 'commit': sha,
                     'snapshot_date': stamp.date().isoformat(), 'snapshot_at': stamp.isoformat(),
                     'url': f'https://github.com/{repo}/blob/{sha}/{path}'}


def fetch_policy_history(config, latest_records, latest):
    """Newest revision wins even if it is fatal, withdrawn, or no longer policy."""
    from .weekly_policy import now_kst, source_identity
    start = datetime.combine(today_kst() - timedelta(days=4), time.min,
                             tzinfo=ZoneInfo('Asia/Seoul'))
    limit = config.get('weekly_policy', {}).get('max_history_exports', 20)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Invalid policy history bound')
    repo = config['repository']
    commits = _get_json(f'https://api.github.com/repos/{repo}/commits?'
                       f'path=data%2Fposts_latest.json&sha={latest["commit"]}'
                       f'&since={quote(start.isoformat(), safe="")}&per_page={limit + 1}')
    if not isinstance(commits, list) or not commits or len(commits) > limit:
        raise ValueError('Policy history incomplete or exceeds bounded window')
    if commits[0]['sha'] != latest['commit']:
        raise ValueError('Policy history does not match readiness snapshot')
    records, seen_ids, seen_sources, seen_shas = [], set(), set(), set()
    previous = now_kst()
    for commit in commits:
        sha = commit['sha']
        stamp = datetime.fromisoformat(commit['commit']['committer']['date'])
        if stamp.tzinfo is None or not start <= stamp <= previous or sha in seen_shas:
            raise ValueError('Invalid policy history order or timestamp')
        previous = stamp
        seen_shas.add(sha)
        rows, origin = ((latest_records, latest) if sha == latest['commit']
                        else _community_snapshot(repo, commit))
        # Duplicate identities within one export are ambiguous, so suppress all copies.
        ids = [r.get('id') for r in rows]
        urls = [source_identity(r['src']) if r.get('src') else None for r in rows]
        for record in rows:
            identity = record.get('id')
            if not isinstance(identity, str) or not identity:
                raise ValueError('Policy history contains an unidentifiable record')
            url = source_identity(record['src']) if record.get('src') else None
            duplicate = identity in seen_ids or (url and url in seen_sources)
            seen_ids.add(identity)
            if url:
                seen_sources.add(url)
            if duplicate or ids.count(identity) > 1 or (url and urls.count(url) > 1):
                continue
            if (record.get('retracted') or record.get('withdrawn')
                    or record.get('status') in {'retracted', 'withdrawn', 'superseded'}):
                continue
            records.append((record, {**origin, 'latest_snapshot': latest}))
    return records


def community_request(record: dict, provenance: dict, config: dict, *, policy_evidence=None) -> ContentRequest | None:
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
            r"매출|영업이익|순이익|증설|생산능력|IR\s*간담회)",
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
        r"(?m)^(?:발간일|발행일|공시일|접수일|보도\s*시각|보도일|자료\s*기준일|기준일)"
        r"[ \t]*[:：]?[ \t]*(20\d{2})-?(\d{2})-?(\d{2})\b", record["facts"])
    dates += re.findall(
        r"(?m)^발간[ \t]*[:：][^\r\n]*?[/／][ \t]*(20\d{2})-?(\d{2})-?(\d{2})\b",
        record["facts"])
    if not dates:
        return None
    data_date = min(date.fromisoformat('-'.join(d)) for d in dates)
    age_limit = int(config.get("max_age_days", 1))
    if policy_evidence is None:
        if not (0 <= (today_kst() - stamp).days <= age_limit
                and 0 <= (today_kst() - data_date).days <= age_limit):
            return None
    else:
        if record.get('kind') != 'policy':
            return None
        data_date = date.fromisoformat(policy_evidence['event']['date'])
        if not (0 <= (today_kst() - stamp).days <= 4
                and 0 <= (today_kst() - data_date).days <= 4 and data_date <= stamp):
            return None
    # Strip assignee, private diagnostics and raw model tails.
    data = {key: record.get(key, "") for key in (
        "id", "kind", "stock_code", "stock_name", "title", "facts", "src", "body",
    )}
    data["source_date"] = data_date.isoformat()
    if sector_only:
        data.update(stock_name="", stock_code="", sector_only=True)
    key = hashlib.sha256(f"{provenance['repository']}:{record['id']}".encode()).hexdigest()[:24]
    metadata = {**provenance, 'source_date': data_date.isoformat(), 'source_url': record['src'],
                'source_record_id': record['id'], 'source_score': dict(score)}
    if policy_evidence is not None:
        from .weekly_policy import VERSION, record_digest, source_identity
        data['policy_evidence'] = policy_evidence
        event = policy_evidence['event']
        event_key = hashlib.sha256((source_identity(event['source']['url']) + ':'
                                    + event['date']).encode()).hexdigest()
        metadata.update(investment_mode=VERSION, policy_event_key=event_key,
                        source_url=source_identity(record["src"]),
                        source_record_sha256=record_digest(record),
                        policy_evidence=policy_evidence)
    return ContentRequest(f'community-{key}', 'investment', data, provenance=metadata)


def collect_requests(settings: Settings) -> tuple[list[ContentRequest], list[dict]]:
    requests, notices = [], []
    for path in sorted(settings.inbox_dir.glob("*/request.json")):
        try:
            request = ContentRequest(**json.loads(path.read_text(encoding="utf-8")))
            if request.category == "cooking":
                if not all(request.data.get(k) for k in ("name", "ingredients", "steps")):
                    raise ValueError("Recipe incomplete")
                verify_photos(request.photos)
            elif request.category == 'investment':
                from .life_economics import request_contract
                if not request_contract(request, fresh=False):
                    raise ValueError('Investment queue accepts only owner life-economics questions')
            elif request.category not in {"parenting", "exercise", "origins"} or not request.data.get("question"):
                raise ValueError("Question missing or unsupported category")
            editorial_type(request.category, request.data)
            if request.category == 'origins':
                verify_review_transport_budget(settings)
                if len(request.photos) != 1:
                    raise ValueError('Origins accepts one thumbnail')
                verify_photos(request.photos)
                for photo in request.photos:
                    origin_photo_metadata(photo)
                    if (Path(photo['file']).resolve().parent != path.parent.resolve()
                            or Path(photo['file']).is_symlink()
                            or not re.fullmatch(r'photo-\d+\.(?:jpg|jpeg|png|webp)', Path(photo['file']).name)):
                        raise ValueError('Origins thumbnail is outside its managed queue')
            if content_style(request.category, request.data) == 'review':
                verify_review_transport_budget(settings)
                verify_photos(request.photos)
                for photo in request.photos:
                    review_photo_metadata(photo)
                    if (Path(photo['file']).resolve().parent != path.parent.resolve()
                            or Path(photo['file']).is_symlink()
                            or not re.fullmatch(r'photo-\d+\.(?:jpg|jpeg|png|webp)', Path(photo['file']).name)):
                        raise ValueError('Review photo is outside its managed queue')
                if not any(p.get('origin', 'owner') == 'owner' for p in request.photos):
                    raise ValueError('Review requires an actual owner photo')
            requests.append(request)
        except (OSError, ValueError, TypeError, KeyError):
            notices.append({"status": "INPUT_REJECTED"})
    # A dated, explicitly selected public topic replaces that category's daily queue.
    scheduled = settings.config.get("topics", {}).get("scheduled", {}).get(str(today_kst()))
    if scheduled:
        request = ContentRequest(**scheduled)
        if (request.category not in {"parenting", "exercise", "origins"}
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", request.id)
                or not request.data.get("question")):
            raise ValueError("Invalid scheduled topic")
        editorial_type(request.category, request.data)
        if request.category == 'origins' or content_style(request.category, request.data) == 'review':
            raise ValueError('Supplied photos must be registered through the private input queue')
        requests = [request, *[r for r in requests if r.category != request.category]]
    context_path = settings.db_path.parent / "context.json"
    context = json.loads(context_path.read_text()) if context_path.exists() else {}
    config = settings.config.get("community", {})
    plan = active_plan(settings)
    if plan:
        requests = [r for r in requests if matches_request(r, plan)]
    if config.get("enabled", False) and (not plan or (plan['category'] == 'investment'
            and plan.get('investment_mode') != 'life-economics-v1')):
        try:
            records, provenance = fetch_community(config)
            from .weekly_policy import enabled
            if enabled(settings) or config.get("require_today_snapshot", True):
                stamp = datetime.fromisoformat(provenance["snapshot_at"])
                stamp = stamp.astimezone(ZoneInfo("Asia/Seoul"))
                ready_hour = config.get('snapshot_ready_hour', 7)
                if type(ready_hour) is not int or not 0 <= ready_hour <= 23:
                    raise ValueError('Invalid community snapshot readiness hour')
                if stamp.date() != today_kst() or stamp.hour < ready_hour:
                    notices.append({"status": "COMMUNITY_SOURCE_PENDING"})
                    return requests, notices
            from .weekly_policy import enabled, evidence_for, now_kst
            weekly = enabled(settings)
            if weekly:
                if stamp.tzinfo is None or stamp > now_kst():
                    raise ValueError('Future or naive readiness snapshot')
                candidates = fetch_policy_history(config, records, provenance)
            else:
                candidates = [(r, provenance) for r in records]
            eligible = 0
            for record, origin in candidates:
                if today_kst().isoformat() <= context.get("exclude_investment_topics_until", ""):
                    source_text = " ".join(str(record.get(k, "")) for k in ["stock_name", "title", "facts"])
                    if any(topic in source_text for topic in context.get("excluded_investment_topics", [])):
                        continue
                try:
                    if weekly and record.get('kind') != 'policy':
                        continue
                    evidence = evidence_for(settings, record, origin) if weekly else None
                    request = community_request(record, origin, config, policy_evidence=evidence)
                    if request:
                        requests.append(request)
                        eligible += 1
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue  # Malformed one-off records do not block the owner's input.
            if not eligible:
                notices.append({"status": "NO_ELIGIBLE_INVESTMENT", "source_count": len(records),
                                "reason": ("weekly_policy_evidence_or_source_not_eligible" if weekly
                                           else "source_date_score_or_issue_not_eligible")})
        except (OSError, ValueError, TypeError, KeyError) as exc:
            notices.append({"status": "COMMUNITY_SOURCE_UNAVAILABLE", "error": type(exc).__name__,
                            "http_status": getattr(exc, 'code', None)})
    if plan and not requests and not notices:
        notices.append({'status': 'PLANNED_INPUT_REQUIRED', 'category': plan['category'],
                        'editorial_types': plan['editorial_types']})
    return requests, notices
