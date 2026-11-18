"""Validate Seonoo review records and local evidence; does not judge prose or images."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
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


def validate(record, request_id, current_revision, artifact_root=None):
    """Check structure, local file hashes and quote presence, never semantic truth."""
    if not isinstance(record, dict):
        return {"status": "unscored", "total": None, "quality_decision": "unverified",
                "reasons": ["record"], "scope": "record_and_evidence_only"}
    invalid = []
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
        if stage in REVIEWS or stage == "naver_save":
            if not revision_hash(step.get("revision")):
                invalid.append(stage + ".revision")
        if stage in PREVIOUS_REVISIONS:
            if step.get("draft_id") != draft_id:
                invalid.append(stage + ".draft_id")
            prior = steps.get(PREVIOUS_REVISIONS[stage])
            prior_revision = prior.get("revision") if isinstance(prior, dict) else None
            if not revision_hash(prior_revision) or step.get("input_revision") != prior_revision:
                invalid.append(stage + ".input_revision")
            if stage in REVIEWS and isinstance(step.get("changes"), list):
                if bool(step["changes"]) == (step.get("revision") == prior_revision):
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
    image_widths = {}
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
                    image_widths[key] = picture.width
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
                parsed.port
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
            if stage in REVIEWS and step.get("changes"):
                if quality_evidence(step["changes"]) is None:
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
            if valid_score and score < 80:
                if (not evidence(deductions) or quality_evidence(deductions) is None
                        or not text(item.get("immediate_fix"))):
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
                        or image_widths.get(key, 0) < minimum_width
                        or screen["sha256"] in media_hashes or screen["sha256"] in seen_screens):
                    invalid.append("screen." + role)
                seen_screens.add(screen["sha256"])

    unresolved = record.get("unresolved")
    if not isinstance(unresolved, list) or (unresolved and not evidence(unresolved)):
        invalid.append("unresolved")
    elif (bool({"revise", "rewrite"}.intersection(states)) or "fail" in hard_states) and not unresolved:
        invalid.append("quality.unresolved_mismatch")
    if invalid:
        return {"status": "unscored", "total": None, "quality_decision": "unverified",
                "reasons": invalid, "scope": "record_and_evidence_only"}
    reasons = []
    total = sum(scores) / 5 if len(scores) == 5 else None
    if bool({"revise", "rewrite"}.intersection(states)) or unresolved or "fail" in hard_states:
        reasons.append("unresolved_defects")
    if "unverified" in states or "unverified" in hard_states:
        reasons.append("unverified_quality")
        total = None
    if total is not None and total < 85:
        reasons.append("average_below_85")
    if scores and min(scores) < 80:
        reasons.append("persona_below_80")
    decision = "unverified" if "unverified_quality" in reasons else ("revise" if reasons else "pass")
    return {"status": "record_valid", "total": total, "quality_decision": decision,
            "reasons": reasons, "scope": "record_and_evidence_only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record", type=Path)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--artifact-root", type=Path)
    args = parser.parse_args()
    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            raise ValueError("The record must be a JSON object.")
        result = validate(record, args.request_id, args.revision, args.artifact_root)
    except (OSError, ValueError) as exc:
        result = {"status": "unscored", "total": None, "quality_decision": "unverified",
                  "reasons": [str(exc)], "scope": "record_and_evidence_only"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "record_valid" and result["quality_decision"] == "pass" else (
        1 if result["status"] == "record_valid" else 2)


if __name__ == "__main__":
    raise SystemExit(main())
