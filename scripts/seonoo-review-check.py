"""Validate Seonoo review records and local evidence; does not judge prose or images."""
import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

try:
    from PIL import Image
except ImportError:
    Image = None

STANDARD = "seonoo-production-v7"
STAGES = ("candidate", "draft", "media", "first_review", "naver_save",
          "second_review", "mobile_review")
REVIEWS = ("first_review", "second_review", "mobile_review")
PREVIOUS_REVISIONS = {"naver_save": "first_review", "second_review": "naver_save",
                      "mobile_review": "second_review"}
AXES = ("structure", "voice", "design", "topic", "reader")
HARD_CHECKS = ("facts", "experience", "media_match", "representative", "mobile", "disclosure")


def text(value):
    return isinstance(value, str) and bool(value.strip())


def evidence(entries):
    return (isinstance(entries, list) and bool(entries) and all(
        isinstance(item, dict) and text(item.get("location"))
        and text(item.get("observation")) for item in entries))


def revision_hash(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def timestamp(value):
    if not text(value):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def slot_state(scheduled_at, started_at, now, draft_id=None, save_uncertain=False):
    if started_at < scheduled_at:
        raise ValueError("started_at precedes scheduled_at")
    elapsed = now - scheduled_at
    if now < started_at or elapsed >= timedelta(minutes=60):
        phase = "closed"
    elif elapsed >= timedelta(minutes=55):
        phase = "record_only"
    elif elapsed >= timedelta(minutes=40):
        phase = "closeout"
    else:
        phase = "edit"
    if phase in ("closed", "record_only"):
        action = "stop" if phase == "closed" else "record_handoff"
    elif save_uncertain:
        action = "reconcile_save"
    elif draft_id and phase == "closeout":
        action = "verify_saved_draft"
    elif draft_id:
        action = "resume_draft"
    else:
        action = "create_draft" if phase == "edit" else "handoff"
    return {"phase": phase, "action": action, "draft_id": draft_id}


def json_artifact(checked, key):
    pair = checked.get(key) if isinstance(key, str) else None
    if pair is None or pair[0]["kind"] != "text":
        return {}, None
    try:
        value = json.loads(pair[1])
    except ValueError:
        return {}, None
    if not isinstance(value, dict):
        return {}, None
    return value, pair[0]["sha256"]


def http_url(value):
    if not text(value) or any(char.isspace() for char in value):
        return False
    try:
        url = urlsplit(value)
        return url.scheme in ("http", "https") and bool(url.hostname) and url.username is None
    except ValueError:
        return False


def check_saved_dom(dom, saved, draft_id, revision, earliest, reopened, purpose_urls):
    observed = timestamp(dom.get("observed_at"))
    if (dom.get("draft_id") != draft_id or dom.get("revision") != revision
            or not earliest or not observed or not reopened or not earliest <= observed <= reopened
            or dom.get("title") != saved.get("title") or dom.get("quotes") != saved.get("quotes")
            or dom.get("purpose_urls") != purpose_urls
            or not all(isinstance(dom.get(key), list)
                       for key in ("body_images", "links", "quotes", "purpose_urls"))):
        return [], ["saved_dom.evidence_unverified"]

    defects, unavailable = [], []
    if dom["title"] != dom["title"].strip():
        defects.append("saved_dom.title_whitespace")
    if dom.get("media_settled") is not True:
        unavailable.append("saved_dom.media_loading")
    svg_count = dom.get("svg_count")
    if type(svg_count) is not int or svg_count < 0:
        unavailable.append("saved_dom.svg_count_unverified")
    elif dom.get("media_settled") is True and svg_count != 0:
        defects.append("saved_dom.inline_svg")

    images = dom["body_images"]
    expected_images = saved.get("images")
    if (not isinstance(expected_images, list)
            or not all(isinstance(img, dict) for img in images + expected_images)
            or len(images) != len(expected_images)):
        unavailable.append("saved_dom.image_coverage")
    else:
        seen_urls = set()
        seen_hashes = set()
        for img, expected in zip(images, expected_images):
            url, loaded, digest = img.get("src"), img.get("loaded"), img.get("sha256")
            if digest is not None and (not revision_hash(digest) or digest != expected.get("sha256")):
                unavailable.append("saved_dom.image_revision")
            if dom.get("media_settled") is not True:
                continue
            if not http_url(url) or loaded is False:
                defects.append("saved_dom.image_not_loaded")
            elif loaded is not True:
                unavailable.append("saved_dom.image_load_unverified")
            elif not urlsplit(url).hostname.endswith(".pstatic.net"):
                unavailable.append("saved_dom.upload_host_unverified")
            if (isinstance(url, str) and url in seen_urls) or expected["sha256"] in seen_hashes:
                defects.append("saved_dom.duplicate_image")
            if isinstance(url, str):
                seen_urls.add(url)
            seen_hashes.add(expected["sha256"])

    approved = dom["purpose_urls"]
    links = dom["links"]
    if (type(dom.get("purpose_required")) is not bool
            or not all(http_url(url) for url in approved)
            or (dom["purpose_required"] and not approved)
            or bool(approved) != dom["purpose_required"]
            or not all(isinstance(link, dict) and http_url(link.get("href"))
                       and (link.get("destination") is None or http_url(link.get("destination")))
                       for link in links)):
        unavailable.append("saved_dom.purpose_evidence")
    elif saved.get("links") != [link["href"] for link in links]:
        unavailable.append("saved_dom.link_coverage")
    else:
        for url in approved:
            if any(link.get("destination") == url for link in links):
                continue
            if any(link.get("destination") is None for link in links):
                unavailable.append("saved_dom.purpose_destination_unverified")
            else:
                defects.append("saved_dom.purpose_link_missing_or_wrong")

    for quote in dom["quotes"]:
        if (not isinstance(quote, dict) or not text(quote.get("text"))
                or type(quote.get("source_placeholder_visible")) is not bool
                or not isinstance(quote.get("adjacent_headings"), list)
                or not all(isinstance(line, str) for line in quote["adjacent_headings"])):
            unavailable.append("saved_dom.quote_evidence")
            continue
        if quote["source_placeholder_visible"]:
            defects.append("saved_dom.quote_placeholder")
        if " ".join(quote["text"].split()) in {
            " ".join(heading.split()) for heading in quote["adjacent_headings"]
        }:
            defects.append("saved_dom.duplicate_heading")
    return list(dict.fromkeys(defects)), list(dict.fromkeys(unavailable))


def blind_input(snapshot_path, draft_id, standard_commit, screenshots, references):
    raw = snapshot_path.read_bytes()
    saved = json.loads(raw)
    if (not isinstance(saved, dict) or not text(saved.get("title")) or not text(saved.get("body"))
            or not text(draft_id) or not re.fullmatch(r"[0-9a-f]{40}", standard_commit)):
        raise ValueError("invalid blind input identity or snapshot")
    roles = {"list_140", "mobile_360"} | ({"mobile_table"} if saved.get("tables") else set())
    if set(screenshots) != roles:
        raise ValueError("blind input screens do not match saved draft")
    if not isinstance(references, list) or not all(
            isinstance(ref, dict) and set(ref) == {"url", "excerpt"}
            and http_url(ref["url"]) and text(ref["excerpt"]) for ref in references):
        raise ValueError("invalid official references")
    return {
        "standard_commit": standard_commit,
        "draft_id": draft_id,
        "revision": hashlib.sha256(raw).hexdigest(),
        "title": saved["title"],
        "body": saved["body"],
        "screens": {role: hashlib.sha256(path.read_bytes()).hexdigest()
                    for role, path in screenshots.items()},
        "official_references": references,
    }


def check_blind_review(record, saved, checked, standard_commit, earliest, reopened):
    blind = record.get("blind_review")
    if not isinstance(blind, dict):
        return ["blind_review.evidence_unverified"], []
    payload, input_sha = json_artifact(checked, blind.get("input_artifact"))
    result, result_sha = json_artifact(checked, blind.get("result_artifact"))
    if input_sha is None or result_sha is None:
        return ["blind_review.evidence_unverified"], []
    step = record["steps"].get("second_review")
    if not isinstance(step, dict):
        return ["blind_review.evidence_unverified"], []
    revision, draft_id = record["scored_revision"], record["draft_id"]
    if (set(payload) != {"standard_commit", "draft_id", "revision", "title", "body",
                         "screens", "official_references"}
            or payload.get("standard_commit") != standard_commit
            or payload.get("draft_id") != draft_id or payload.get("revision") != revision
            or step.get("input_revision") != revision or step.get("revision") != revision
            or payload.get("title") != saved.get("title") or payload.get("body") != saved.get("body")
            or not isinstance(payload.get("official_references"), list)
            or not all(isinstance(ref, dict) and set(ref) == {"url", "excerpt"}
                       and http_url(ref["url"]) and text(ref["excerpt"])
                       for ref in payload["official_references"])):
        return [], ["blind_review.input"]
    screens = payload["screens"]
    roles = {"list_140", "mobile_360"} | ({"mobile_table"} if saved.get("tables") else set())
    expected_screens = {
        data["role"]: data["sha256"] for data, _ in checked.values()
        if isinstance(data.get("role"), str) and data["role"] in roles and data.get("revision") == revision
        and data.get("draft_id") == draft_id and data["kind"] == "image"
    }
    if not isinstance(screens, dict) or set(screens) != roles or screens != expected_screens:
        return [], ["blind_review.screens"]

    prepared = timestamp(blind.get("input_prepared_at"))
    started = timestamp(result.get("started_at"))
    finished = timestamp(result.get("finished_at"))
    compared = timestamp(blind.get("compared_first_at"))
    if (result.get("isolation") != "stateless_no_tools" or not text(result.get("call_id"))
            or not all((earliest, prepared, started, finished, compared, reopened))):
        return ["blind_review.isolation_unverified"], []
    screen_artifacts = [data for data, _ in checked.values()
                        if isinstance(data.get("role"), str) and data["role"] in roles and data["sha256"] == screens[data["role"]]]
    captures = [timestamp(data.get("captured_at")) for data in screen_artifacts]
    if (not earliest <= prepared <= started <= finished < compared
            or any(at is None or not earliest <= at <= min(prepared, reopened) for at in captures)
            or result.get("input_sha256") != input_sha or result.get("scores") != step.get("scores")
            or timestamp(step.get("at")) != finished):
        return [], ["blind_review.result"]
    return [], []


def check_frozen_scores(record, history, checked):
    missing, invalid = [], []
    if not isinstance(history, list) or not all(isinstance(row, dict) for row in history):
        return ["review_history.unverified"], []
    for stage in REVIEWS:
        step = record["steps"].get(stage)
        if not isinstance(step, dict):
            continue
        matches = [row for row in history
                   if row.get("request_id") == record.get("request_id")
                   and row.get("draft_id") == record.get("draft_id")
                   and row.get("revision") == step.get("revision") and row.get("stage") == stage]
        if not matches:
            missing.append(stage + ".freeze_unverified")
        elif len(matches) != 1 or matches[0].get("scores") != step.get("scores") or (
                timestamp(matches[0].get("at")) != timestamp(step.get("at"))):
            invalid.append(stage + ".frozen_scores_changed")
        elif stage == "second_review":
            blind = record.get("blind_review")
            _, digest = json_artifact(checked, blind.get("result_artifact")
                                      if isinstance(blind, dict) else None)
            frozen = timestamp(matches[0].get("frozen_at"))
            finished = timestamp(step.get("at"))
            compared = timestamp(blind.get("compared_first_at")) if isinstance(blind, dict) else None
            if digest is None or frozen is None or compared is None:
                missing.append(stage + ".freeze_unverified")
            elif matches[0].get("result_sha256") != digest:
                invalid.append(stage + ".frozen_result_changed")
            elif not finished or not finished <= frozen < compared:
                invalid.append(stage + ".freeze_order")
    return missing, invalid


def validate(record, request_id, current_revision, artifact_root=None, *,
             standard_commit=None, review_history=None):
    """Check structure, local file hashes and quote presence, never semantic truth."""
    if not isinstance(record, dict):
        return {"status": "unscored", "total": None, "quality_decision": "unverified",
                "reasons": ["record"], "scope": "record_and_evidence_only"}
    invalid = []
    guarded = standard_commit is not None or "standard_commit" in record
    if guarded and (not isinstance(standard_commit, str)
                    or not re.fullmatch(r"[0-9a-f]{40}", standard_commit)
                    or record.get("standard_commit") != standard_commit):
        invalid.append("standard_commit")
    if record.get("standard_version") != STANDARD:
        invalid.append("standard_version")
    if record.get("request_id") != request_id or not text(request_id):
        invalid.append("request_id")
    draft_id = record.get("draft_id")
    if not text(draft_id):
        invalid.append("draft_id")
    if not revision_hash(current_revision):
        invalid.append("current_revision")
    steps = record.get("steps")
    if not isinstance(steps, dict):
        steps = {}
    previous = None
    for stage in STAGES:
        step = steps.get(stage)
        if not isinstance(step, dict):
            invalid.append(stage)
            continue
        at = timestamp(step.get("at"))
        if at is None or (previous and at < previous) or not evidence(step.get("evidence")):
            invalid.append(stage)
        if at is not None:
            previous = at
        if stage in REVIEWS and step.get("draft_id") != draft_id:
            invalid.append(stage + ".draft_id")
        if stage in REVIEWS:
            changes = step.get("changes")
            if not isinstance(changes, list) or (changes and not evidence(changes)):
                invalid.append(stage + ".changes")
        if (stage in REVIEWS or stage == "naver_save") and not revision_hash(step.get("revision")):
            invalid.append(stage + ".revision")
        if stage in PREVIOUS_REVISIONS:
            if step.get("draft_id") != draft_id:
                invalid.append(stage + ".draft_id")
            prior = steps.get(PREVIOUS_REVISIONS[stage])
            prior_revision = prior.get("revision") if isinstance(prior, dict) else None
            if not revision_hash(prior_revision) or step.get("input_revision") != prior_revision:
                invalid.append(stage + ".input_revision")
            if (stage in REVIEWS and isinstance(step.get("changes"), list)
                    and bool(step["changes"]) == (step.get("revision") == prior_revision)):
                invalid.append(stage + ".changes_mismatch")
    mobile = steps.get("mobile_review")
    if isinstance(mobile, dict):
        at, reopened = timestamp(mobile.get("at")), timestamp(mobile.get("reopened_at"))
        if at is None or reopened is None or reopened < at:
            invalid.append("mobile_review.reopened_at")
        if mobile.get("revision") != current_revision:
            invalid.append("mobile_review.stale_revision")
    if record.get("scored_revision") != current_revision:
        invalid.append("quality.stale_revision")

    artifacts = record.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        invalid.append("artifacts")
        artifacts = {}
    checked = {}
    image_sizes = {}
    saved = {}
    root = Path(artifact_root).resolve() if artifact_root is not None else None
    if root is None or not root.is_dir():
        invalid.append("artifact_root")
    for key, item in artifacts.items():
        try:
            if (not isinstance(item, dict) or item.get("kind") not in ("text", "image")
                    or not text(item.get("path")) or not revision_hash(item.get("sha256"))):
                raise ValueError("metadata")
            relative = Path(item["path"])
            if root is None or relative.is_absolute():
                raise ValueError("path")
            path = (root / relative).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError("path")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("hash")
            content = raw.decode("utf-8") if item["kind"] == "text" else None
            if item["kind"] == "image":
                if Image is None:
                    raise ValueError("image_decoder_unavailable")
                with Image.open(path) as picture:
                    picture.verify()
                with Image.open(path) as picture:
                    picture.load()
                    image_sizes[key] = picture.size
            checked[key] = (item, content)
        except (OSError, ValueError, TypeError, SyntaxError) as exc:
            invalid.append("artifacts." + str(key) + "." + str(exc))

    snapshot_id = record.get("snapshot_artifact")
    snapshot = checked.get(snapshot_id) if isinstance(snapshot_id, str) else None
    if snapshot is None or snapshot[0]["kind"] != "text":
        invalid.append("snapshot_artifact")
    else:
        if snapshot[0]["sha256"] != current_revision:
            invalid.append("snapshot.revision")
        try:
            saved = json.loads(snapshot[1])
            if (not isinstance(saved, dict) or not text(saved.get("title"))
                    or not text(saved.get("body"))
                    or not all(isinstance(saved.get(k), list) for k in ("images", "tables", "cards", "tags"))):
                raise ValueError("fields")
            if not all(isinstance(tag, str) and tag.strip() for tag in saved["tags"]):
                raise ValueError("tags")
            for table in saved["tables"]:
                if (not isinstance(table, dict) or not isinstance(table.get("rows"), list)
                        or not table["rows"] or not all(isinstance(row, list) and row
                        and all(isinstance(cell, str) for cell in row) for row in table["rows"])):
                    raise ValueError("tables")
            for card in saved["cards"]:
                if (not isinstance(card, dict) or not text(card.get("title"))
                        or not text(card.get("url"))
                        or not card["url"].startswith(("https://", "http://"))):
                    raise ValueError("cards")
                parsed = urlsplit(card["url"])
                if not parsed.hostname or any(c.isspace() for c in card["url"]):
                    raise ValueError("cards_url")
                _ = parsed.port
            image_hashes = {item["sha256"] for item, _ in checked.values() if item["kind"] == "image"}
            for media in [saved.get("representative"), *saved["images"]]:
                if not isinstance(media, dict) or media.get("sha256") not in image_hashes:
                    raise ValueError("media_hash")
        except (ValueError, TypeError, KeyError) as exc:
            invalid.append("snapshot." + str(exc))

    def quality_evidence(entries):
        if not isinstance(entries, list) or not entries:
            return None
        kinds = set()
        for entry in entries:
            if not isinstance(entry, dict) or not text(entry.get("observation")):
                return None
            artifact_id = entry.get("artifact")
            if not isinstance(artifact_id, str):
                return None
            artifact = checked.get(artifact_id)
            if artifact is None:
                return None
            item, content = artifact
            if item["kind"] == "text":
                quote = entry.get("quote")
                if not text(quote) or quote not in content:
                    return None
            kinds.add(item["kind"])
        return kinds

    for stage in STAGES:
        step = steps.get(stage)
        if isinstance(step, dict):
            if quality_evidence(step.get("evidence")) is None:
                invalid.append(stage + ".artifact_evidence")
            if stage in REVIEWS and step.get("changes") and quality_evidence(step["changes"]) is None:
                invalid.append(stage + ".changes_artifact_evidence")

    quality = record.get("quality")
    if not isinstance(quality, dict) or set(quality) != set(AXES):
        invalid.append("quality")
        quality = {}
    states = []
    scores = []
    for axis, item in quality.items():
        if not isinstance(item, dict) or item.get("status") not in ("pass", "revise", "rewrite", "unverified"):
            invalid.append("quality." + axis)
            continue
        status = item["status"]
        states.append(status)
        score, sub = item.get("score"), item.get("subscores")
        if status == "unverified":
            if score is not None or sub is not None or not text(item.get("reason")):
                invalid.append("quality." + axis + ".unverified_score")
        else:
            valid_score = type(score) is int and 0 <= score <= 100
            if (not valid_score or not isinstance(sub, list) or len(sub) != 5
                    or any(type(v) is not int or not 0 <= v <= 20 for v in sub)
                    or sum(sub) != score):
                invalid.append("quality." + axis + ".score")
            else:
                scores.append(score)
                expected = "pass" if score >= 80 else ("revise" if score >= 60 else "rewrite")
                if status != expected:
                    invalid.append("quality." + axis + ".score_status")
            deductions = item.get("deductions")
            if not isinstance(deductions, list):
                invalid.append("quality." + axis + ".deductions")
            if (valid_score and score < 80 and (not evidence(deductions)
                    or quality_evidence(deductions) is None or not text(item.get("immediate_fix")))):
                invalid.append("quality." + axis + ".fix")
        if status == "unverified" and not item.get("evidence") and text(item.get("reason")):
            continue
        kinds = quality_evidence(item.get("evidence"))
        if kinds is None:
            invalid.append("quality." + axis + ".evidence")
        elif status == "pass":
            if not any(e.get("artifact") == snapshot_id for e in item["evidence"]):
                invalid.append("quality." + axis + ".current_snapshot_evidence")
            if "text" not in kinds:
                invalid.append("quality." + axis + ".text_evidence")
            if axis in ("design", "structure") and "image" not in kinds:
                invalid.append("quality." + axis + ".image_evidence")
        if axis == "voice" and status == "pass":
            refs = quality_evidence(item.get("reference_evidence"))
            if refs is None or "text" not in refs:
                invalid.append("quality.voice.reference_evidence")
            elif snapshot is not None and any(checked[e["artifact"]][0]["sha256"] == current_revision
                     or checked[e["artifact"]][0]["path"] == snapshot[0]["path"]
                     for e in item["reference_evidence"]):
                invalid.append("quality.voice.reference_is_current_draft")

    def fresh_correction(entries, before, after):
        if not evidence(entries) or not quality_evidence(entries):
            return False
        start, end = timestamp(before.get("at")), timestamp(after.get("at"))
        for entry in entries:
            artifact = checked[entry["artifact"]][0]
            at = timestamp(artifact.get("captured_at"))
            if (start and end and at and start < at <= end
                    and artifact.get("draft_id") == draft_id
                    and artifact.get("revision") == after.get("revision")
                    and artifact["sha256"] not in prior_evidence_hashes):
                return True
        return False

    earlier_review = None
    prior_evidence_hashes = set()
    for stage in REVIEWS:
        step = steps.get(stage)
        if not isinstance(step, dict):
            continue
        stage_scores = step.get("scores")
        if (not isinstance(stage_scores, dict) or set(stage_scores) != set(AXES)
                or any(v is not None and (type(v) is not int or not 0 <= v <= 100)
                       for v in stage_scores.values())):
            invalid.append(stage + ".scores")
        elif stage == "mobile_review" and any(
                stage_scores[a] != (quality[a].get("score") if isinstance(quality.get(a), dict) else None) for a in AXES):
            invalid.append("mobile_review.score_mismatch")

        else:
            if stage != "mobile_review" and any(value is None for value in stage_scores.values()):
                invalid.append(stage + ".incomplete_scores")
            if earlier_review is not None:
                previous_scores = earlier_review.get("scores")
                same_copy = step.get("revision") == earlier_review.get("revision")
                increases = [axis for axis in AXES if isinstance(previous_scores, dict)
                             and type(previous_scores.get(axis)) is int
                             and type(stage_scores[axis]) is int
                             and stage_scores[axis] > previous_scores[axis]]
                for axis in increases:
                    if guarded and stage == "second_review" and same_copy:
                        # An independent second round has its own immutable result.
                        continue
                    if guarded and stage == "mobile_review" and same_copy:
                        invalid.append(stage + ".unsupported_increase." + axis)
                        continue
                    if not same_copy and step.get("changes") and quality_evidence(step["changes"]):
                        continue
                    adjustments = step.get("score_corrections")
                    entries = adjustments.get(axis) if isinstance(adjustments, dict) else None
                    if not fresh_correction(entries, earlier_review, step):
                        invalid.append(stage + ".unsupported_increase." + axis)
            earlier_review = step
            groups = [step.get("evidence"), step.get("changes")]
            corrections = step.get("score_corrections")
            if isinstance(corrections, dict):
                groups.extend(corrections.values())
            for entries in groups:
                if quality_evidence(entries):
                    prior_evidence_hashes.update(checked[e["artifact"]][0]["sha256"] for e in entries)

    checks = record.get("hard_checks")
    hard_states = []
    if not isinstance(checks, dict) or set(checks) != set(HARD_CHECKS):
        invalid.append("hard_checks")
        checks = {}
    for key, item in checks.items():
        if not isinstance(item, dict):
            invalid.append("hard_checks." + key)
            continue
        status = item.get("status")
        if status not in ("pass", "fail", "unverified", "not_applicable"):
            invalid.append("hard_checks." + key + ".status")
        hard_states.append(status)
        if status == "not_applicable":
            if key != "disclosure" or not text(item.get("reason")):
                invalid.append("hard_checks." + key + ".not_applicable")
        elif status == "unverified":
            if not text(item.get("reason")):
                invalid.append("hard_checks." + key + ".reason")
        elif quality_evidence(item.get("evidence")) is None:
            invalid.append("hard_checks." + key + ".evidence")

    # A cover alone cannot establish what the saved editor or mobile view displayed.
    if isinstance(saved, dict):
        media_items = [saved.get("representative"), *(saved.get("images") or [])] if isinstance(
            saved.get("images"), list) else []
        media_hashes = {item["sha256"] for item in media_items
                        if isinstance(item, dict) and revision_hash(item.get("sha256"))}
        seen_screens = set()
        latest_save = steps.get("naver_save")
        latest_save_at = timestamp(latest_save.get("at")) if isinstance(latest_save, dict) else None
        for stage in ("second_review", "mobile_review"):
            reviewed = steps.get(stage)
            if isinstance(reviewed, dict) and reviewed.get("changes"):
                latest_save_at = timestamp(reviewed.get("at"))
        mobile_roles = ("mobile_360", "mobile_table") if saved.get("tables") else ("mobile_360",)
        for name, roles in (("representative", ("pc_representative", "list_140")),
                            ("mobile", mobile_roles)):
            check = checks.get(name)
            if not isinstance(check, dict) or check.get("status") != "pass":
                continue
            if name == "representative":
                approved = artifacts.get(record.get("approved_cover")) if isinstance(
                    record.get("approved_cover"), str) else None
                representative = saved.get("representative")
                if (not isinstance(approved, dict) or approved.get("kind") != "image"
                        or not isinstance(representative, dict)
                        or approved.get("sha256") != representative.get("sha256")):
                    invalid.append("representative.approved_cover")
            for role in roles:
                refs = check.get("evidence")
                matches = []
                for ref in refs if isinstance(refs, list) else []:
                    key = ref.get("artifact") if isinstance(ref, dict) else None
                    pair = checked.get(key) if isinstance(key, str) else None
                    if pair and pair[0].get("role") == role:
                        matches.append((key, pair[0]))
                if len(matches) != 1:
                    invalid.append("screen." + role)
                    continue
                key, screen = matches[0]
                observed = timestamp(screen.get("captured_at"))
                minimum_width = 360 if name == "mobile" else 140
                reopened = timestamp(mobile.get("reopened_at")) if isinstance(mobile, dict) else None
                if (screen["kind"] != "image" or screen.get("draft_id") != draft_id
                        or screen.get("revision") != current_revision or latest_save_at is None
                        or observed is None or observed < latest_save_at or reopened is None or observed > reopened
                        or image_sizes.get(key, (0, 0))[0] < minimum_width
                        or screen["sha256"] in media_hashes or screen["sha256"] in seen_screens):
                    invalid.append("screen." + role)
                if guarded and role == "list_140" and (
                        image_sizes.get(key) != (140, 140) or not isinstance(representative, dict)
                        or screen.get("source_sha256") != representative.get("sha256")):
                    invalid.append("screen.list_140_source")
                seen_screens.add(screen["sha256"])

    unresolved = record.get("unresolved")
    if not isinstance(unresolved, list) or (unresolved and not evidence(unresolved)):
        invalid.append("unresolved")
    elif (bool({"revise", "rewrite"}.intersection(states)) or "fail" in hard_states) and not unresolved:
        invalid.append("quality.unresolved_mismatch")
    # Diagnose saved content even when an independent review could not run.
    # These findings do not promote a partial record to quality approval.
    dom_defects, dom_missing = [], ["saved_dom.not_checked"]
    blind = record.get("blind_review")
    blind = blind if isinstance(blind, dict) else {}
    blind_result, _ = json_artifact(checked, blind.get("result_artifact"))
    execution = "not_observed" if not blind_result else "unverified"
    dom, _ = json_artifact(checked, record.get("saved_dom_artifact"))
    if (guarded and isinstance(dom, dict) and saved
            and snapshot is not None and snapshot[0]["sha256"] == current_revision):
        saved_step = steps.get("naver_save")
        saved_step = saved_step if isinstance(saved_step, dict) else {}
        mobile_step = steps.get("mobile_review")
        mobile_step = mobile_step if isinstance(mobile_step, dict) else {}
        candidate_step = steps.get("candidate")
        candidate_step = candidate_step if isinstance(candidate_step, dict) else {}
        boundaries = [timestamp(mobile_step.get("reopened_at")),
                      timestamp(blind.get("input_prepared_at"))]
        boundaries = [at for at in boundaries if at is not None]
        dom_defects, dom_missing = check_saved_dom(
            dom, saved, draft_id, current_revision, timestamp(saved_step.get("at")),
            min(boundaries) if boundaries else None, candidate_step.get("purpose_urls"))
    diagnostics = {
        "saved_dom": {"status": "revise" if dom_defects else (
            "unverified" if dom_missing else "pass"),
            "defects": dom_defects, "missing": dom_missing},
        "independent_review_status": execution,
        "execution_blockers": (["independent_review_not_observed"]
                               if execution == "not_observed" else []),
        "content_status": "revise" if dom_defects else "unverified",
        "publish_ready": False,
        "scope": "partial_diagnostics_not_quality_approval",
    }
    guard_defects, guard_missing = [], []
    if guarded and not invalid:
        save_at = timestamp(steps["naver_save"].get("at"))
        reopened_at = timestamp(steps["mobile_review"].get("reopened_at"))
        dom, _ = json_artifact(checked, record.get("saved_dom_artifact"))
        blind_meta = record.get("blind_review")
        prepared_at = timestamp(blind_meta.get("input_prepared_at")) if isinstance(blind_meta, dict) else None
        dom_deadline = min(prepared_at, reopened_at) if prepared_at and reopened_at else reopened_at
        guard_defects, guard_missing = check_saved_dom(
            dom, saved, draft_id, current_revision, save_at, dom_deadline,
            steps["candidate"].get("purpose_urls"))
        missing, denied = check_blind_review(record, saved, checked, standard_commit, save_at, reopened_at)
        if not missing and not denied:
            diagnostics["independent_review_status"] = "verified"
        guard_missing.extend(missing)
        invalid.extend(denied)
        missing, denied = check_frozen_scores(record, review_history, checked)
        guard_missing.extend(missing)
        invalid.extend(denied)
        if steps["mobile_review"].get("changes"):
            invalid.append("mobile_review.changed_after_blind_review")
    if invalid:
        return {"status": "unscored", "total": None, "quality_decision": "unverified",
                "reasons": invalid, "scope": "record_and_evidence_only",
                "diagnostics": diagnostics}
    reasons = guard_defects + guard_missing
    total = sum(scores) / 5 if len(scores) == 5 else None
    if bool({"revise", "rewrite"}.intersection(states)) or unresolved or "fail" in hard_states or guard_defects:
        reasons.append("unresolved_defects")
    if "unverified" in states or "unverified" in hard_states or guard_missing:
        reasons.append("unverified_quality")
        total = None
    if total is not None and total < 85:
        reasons.append("average_below_85")
    if scores and min(scores) < 80:
        reasons.append("persona_below_80")
    if guard_defects:
        decision = "revise"
    elif "unverified_quality" in reasons:
        decision = "unverified"
    else:
        decision = "revise" if reasons else "pass"
    diagnostics["content_status"] = decision
    # Publication authorization remains a separate operator check.
    return {"status": "record_valid", "total": total, "quality_decision": decision,
            "reasons": reasons, "scope": "record_and_evidence_only",
            "diagnostics": diagnostics}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--blind-input":
        parser = argparse.ArgumentParser(description="Prepare an allowlisted second-round input.")
        parser.add_argument("--snapshot", type=Path, required=True)
        parser.add_argument("--draft-id", required=True)
        parser.add_argument("--standard-commit", required=True)
        parser.add_argument("--list-140", type=Path, required=True)
        parser.add_argument("--mobile-360", type=Path, required=True)
        parser.add_argument("--mobile-table", type=Path)
        parser.add_argument("--official-references", type=Path, required=True)
        args = parser.parse_args(sys.argv[2:])
        try:
            screenshots = {"list_140": args.list_140, "mobile_360": args.mobile_360}
            if args.mobile_table is not None:
                screenshots["mobile_table"] = args.mobile_table
            references = json.loads(args.official_references.read_text(encoding="utf-8"))
            payload = blind_input(args.snapshot, args.draft_id, args.standard_commit, screenshots, references)
        except (OSError, ValueError) as exc:
            parser.error(str(exc))
        sys.stdout.buffer.write(json.dumps(
            payload, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
        return 0

    if len(sys.argv) > 1 and sys.argv[1] == "--slot-state":
        parser = argparse.ArgumentParser(description="Decide a scheduled slot's permitted phase.")
        parser.add_argument("--scheduled-at", required=True)
        parser.add_argument("--started-at", required=True)
        parser.add_argument("--now", required=True)
        parser.add_argument("--draft-id")
        parser.add_argument("--save-uncertain", action="store_true")
        args = parser.parse_args(sys.argv[2:])
        dates = [timestamp(value) for value in (args.scheduled_at, args.started_at, args.now)]
        if None in dates or dates[1] < dates[0]:
            parser.error("slot timestamps must have offsets and start at or after the scheduled time")
        print(json.dumps(slot_state(*dates, args.draft_id, args.save_uncertain), ensure_ascii=False))
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--standard-commit")
    parser.add_argument("--review-history", type=Path)
    args = parser.parse_args()
    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
        history = json.loads(args.review_history.read_text(encoding="utf-8")) if args.review_history else None
        result = validate(record, args.request_id, args.revision, args.artifact_root,
                          standard_commit=args.standard_commit, review_history=history)
    except (OSError, ValueError) as exc:
        result = {"status": "unscored", "total": None, "quality_decision": "unverified",
                  "reasons": [str(exc)], "scope": "record_and_evidence_only"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "record_valid" and result["quality_decision"] == "pass" else (
        1 if result["status"] == "record_valid" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
