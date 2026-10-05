"""Deterministic draft checks and a single, durable pre-review correction slot.

This is not factual approval. The existing strict reviewer still decides whether
claims have read evidence. Only presentation and verified source aliases are
changed without a model; unknown document identities always fail closed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .core import PostDraft, today_kst, validate_post
from .images import atomic_json
from .inputs import ContentRequest
from .presentation import normalize_structure, validate_structure
from .responses import DRAFT_SCHEMA, ResponseFailure

VERSION = 'pre-review-v1'


@dataclass
class DraftCandidate:
    payload: dict
    observed: list[str]


class PreReviewFailure(ResponseFailure):
    def __init__(self, stage: str, codes: list[str]):
        self.codes = list(dict.fromkeys(codes))
        super().__init__(stage, self.codes[0])


def checkpoint_path(directory: Path, request_id: str) -> Path:
    key = hashlib.sha256(request_id.encode()).hexdigest()
    return directory / 'response-cache' / f'{today_kst()}-pre-review-{key}.json'


def revision_path(directory: Path, request_id: str) -> Path:
    return checkpoint_path(directory, request_id).with_suffix('.revision.json')


def claim_revision(directory: Path, request_id: str, stage: str, identity: dict) -> bool:
    """Return cache-only on resume; another stage/input can never buy a second call."""
    path = revision_path(directory, request_id)
    claim = {'stage': stage, 'identity': hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(claim, stream)
            stream.flush()
            os.fsync(stream.fileno())
        return False
    except FileExistsError:
        saved = json.loads(path.read_text())
        if saved != claim:
            raise PreReviewFailure(stage, ['manuscript_correction_limit'])
        return True


def resume_candidate(directory: Path, request: ContentRequest) -> DraftCandidate | None:
    path = checkpoint_path(directory, request.id)
    if not path.exists():
        return None
    saved = json.loads(path.read_text())
    if saved['request'] != request.prompt_data():
        raise PreReviewFailure('pre_review_check', ['pre_review_input_changed'])
    return DraftCandidate(**saved['candidate'])


def candidate_from_post(post: PostDraft) -> DraftCandidate:
    """Only for already source-validated legacy PostDrafts, never raw model output."""
    return DraftCandidate({k: getattr(post, k) for k in DRAFT_SCHEMA['required']},
                          list(post.source_urls))


def _experience_issues(body: str, request: ContentRequest) -> list[str]:
    # Conservative, explicit autobiographical markers only. Contextual truth is
    # still the formal reviewer's job; exact owner-provided sentences are allowed.
    def text_values(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for part in value.values():
                yield from text_values(part)
        elif isinstance(value, list):
            for part in value:
                yield from text_values(part)
    # Source articles/bot summaries are not the owner's own experience.
    owner_context = {k: request.data[k] for k in ('context', 'experience', 'personal_experience')
                     if k in request.data}
    if request.category == 'cooking' and 'tips' in request.data:
        owner_context['tips'] = request.data['tips']
    supplied = [*text_values(owner_context), *[p.get('caption', '') for p in request.photos]]
    normalized = [re.sub(r'\s+', '', text) for text in supplied]
    for sentence in re.split(r'[.!?\n]+', body):
        if (re.search(r'저는|제가|저희|우리\s*(?:집|아이|아기)|선우(?:는|가)', sentence)
                and re.search(r'해봤|해보니|했더니|사용했|써봤|먹여봤|겪었|느꼈|다녀왔|운동했|재워봤', sentence)
                and not any(re.sub(r'\s+', '', sentence.strip()) in text for text in normalized)):
            return ['unsupported_personal_experience']
    for age in re.findall(r'선우(?:는|가)?\s*(\d+)\s*개월', body):
        if request.data.get('age_months') != int(age):
            return ['unsupported_child_age']
    return []


MESSAGES = {
    'Empty draft or invalid subcategory': 'invalid_subcategory_or_empty',
    "Draft must specify today's KST reference date": 'invalid_reference_date',
    'Cooking without owner photos cannot be approved': 'missing_owner_photos',
    'No search-backed sources; hold draft': 'no_sources',
    'Invalid source URL': 'invalid_source_url',
    'Guaranteed-return language requires manual review': 'guaranteed_return_language',
    'Use at least four major sections and a subsection': 'missing_headings',
    'Parenting draft is too short; add supported explanation, not filler': 'body_too_short',
    'Images are placed from verified files, not model URLs': 'inline_image_markup',
    'Start with a plain-language preview summary, not dates or URLs': 'invalid_preview',
}
HARD_STOPS = {'no_observed_evidence', 'unobserved_source_url', 'unobserved_body_url',
              'invalid_source_url', 'missing_owner_photos', 'guaranteed_return_language'}


def inspect_candidate(candidate: DraftCandidate, request: ContentRequest, info: dict,
                      require_structure: bool = True) -> tuple[PostDraft | None, list[str], list[str]]:
    from .llm import _source_identity, _source_urls

    data = dict(candidate.payload)
    codes, changes = [], []
    for name, spec in DRAFT_SCHEMA['properties'].items():
        value = data.get(name)
        valid = (isinstance(value, str) if spec['type'] == 'string' else
                 isinstance(value, list) and all(isinstance(v, str) for v in value))
        if name not in data:
            codes.append(f'missing_field_{name}')
        elif not valid:
            codes.append(f'invalid_field_{name}')
    if set(data) - set(DRAFT_SCHEMA['properties']):
        codes.append('unexpected_fields')
    if (request.category == 'cooking' or request.data.get('content_style') == 'review') and not request.photos:
        codes.append('missing_owner_photos')
    if isinstance(data.get('body'), str) and re.search(
            r'수익\s*보장|원금\s*보장|무조건\s*(상승|매수|매도)|확정\s*수익', data['body']):
        codes.append('guaranteed_return_language')
    observed = list(dict.fromkeys(u for u in candidate.observed if isinstance(u, str)))
    if request.category != 'cooking' and not observed:
        codes.append('no_observed_evidence')
    # Check any well-typed source/body fields even when another field is invalid.
    # A schema defect must never hide an irreparable document-identity failure.
    source_data = {'source_urls': data.get('source_urls', []),
                   'body': data.get('body', '') if isinstance(data.get('body'), str) else ''}
    source_data['source_urls'] = ([u for u in source_data['source_urls'] if isinstance(u, str)]
                                  if isinstance(source_data['source_urls'], list) else [])
    if isinstance(source_data['source_urls'], list) and all(
            isinstance(u, str) for u in source_data['source_urls']):
        try:
            _source_urls(source_data, observed, required=False)
        except ValueError as exc:
            codes.append({'Writer returned no sources': 'no_sources',
                          'Source URL was not present in web-search results': 'unobserved_source_url',
                          'Body contains an unverified URL': 'unobserved_body_url'}.get(
                              str(exc), 'invalid_source_url'))
    if request.category != 'cooking' and not source_data['source_urls']:
        codes.append('no_sources')
    if codes:
        return None, list(dict.fromkeys(codes)), changes

    # Align only identities established by the existing verified alias rules.
    # Never infer equivalence from a common hostname/path or discard query IDs.
    identities = {_source_identity(u): u for u in observed}
    claimed = data['source_urls']
    replacements = {u: identities[_source_identity(u)] for u in claimed
                    if _source_identity(u) in identities and u != identities[_source_identity(u)]}
    for url in re.findall(r'https?://[^\s<>\]\)]+', data['body']):
        url = url.rstrip('.,')
        if _source_identity(url) in identities and url != identities[_source_identity(url)]:
            replacements[url] = identities[_source_identity(url)]
    if replacements:
        data['source_urls'] = [replacements.get(u, u) for u in claimed]
        data['body'] = re.sub(r'https?://[^\s<>\]\)]+',
            lambda m: replacements.get(m[0].rstrip('.,'), m[0].rstrip('.,'))
            + m[0][len(m[0].rstrip('.,')):], data['body'])
        changes.append('observed_source_alignment')
    try:
        urls = _source_urls(data, observed, required=request.category != 'cooking')
    except ValueError as exc:
        code = {'Writer returned no sources': 'no_sources',
                'Source URL was not present in web-search results': 'unobserved_source_url',
                'Body contains an unverified URL': 'unobserved_body_url'}.get(str(exc), 'invalid_source_url')
        return None, [code], changes
    post = PostDraft(request.category, data['subcategory'], data['title'],
                     data['title'].strip(), data['body'].strip(),
                     [t.lstrip('#') for t in data['tags']][:8], urls, data['as_of_date'],
                     request_id=request.id, photos=request.photos, provenance=request.provenance)
    normalized = normalize_structure(post)
    if normalized.body != post.body:
        changes.append('presentation_normalized')
    post = normalized
    for check in (lambda: validate_post(post, info),
                  lambda: validate_structure(post, require_structure)):
        try:
            check()
        except ValueError as exc:
            codes.append(MESSAGES.get(str(exc), 'invalid_reference_date'))
    codes.extend(_experience_issues(post.body, request))
    return post, list(dict.fromkeys(codes)), changes


def _event(request, stage, status, codes=()):
    print(json.dumps({'request_id': request.id, 'stage': stage, 'status': status,
                      'issue_codes': list(codes)}, ensure_ascii=False), flush=True)


def run_pre_review(directory: Path, llm, candidate: DraftCandidate | PostDraft,
                   request: ContentRequest, info: dict, require_structure: bool = True):
    if isinstance(candidate, PostDraft):
        candidate = candidate_from_post(candidate)
    path = checkpoint_path(directory, request.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = {'version': VERSION, 'request': request.prompt_data(), 'info': info,
                'require_structure': require_structure, 'candidate': asdict(candidate)}
    # Exclusive creation also prevents two callers from purchasing a correction.
    saved = {**identity, 'correction_attempted': False}
    try:
        with path.open('x', encoding='utf-8') as stream:
            json.dump(saved, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        saved = json.loads(path.read_text())
        if any(saved.get(k) != v for k, v in identity.items()):
            raise PreReviewFailure('pre_review_check', ['pre_review_input_changed'])
    _event(request, 'pre_review_check', 'PRE_REVIEW_CHECK_STARTED')
    checked = DraftCandidate(**saved['corrected']) if 'corrected' in saved else candidate
    post, codes, changes = inspect_candidate(checked, request, info, require_structure)
    if not codes:
        report = {'status': 'PASS', 'changes': saved.get('changes', []) + changes,
                  'model_correction_used': bool(saved['correction_attempted'])}
        atomic_json(path, {**saved, 'report': report})
        _event(request, 'pre_review_check', 'PRE_REVIEW_PASSED')
        return post, report
    _event(request, 'pre_review_check', 'PRE_REVIEW_ISSUES_FOUND', codes)
    if 'corrected' in saved or set(codes) & HARD_STOPS:
        stage = 'pre_review_recheck' if 'corrected' in saved else 'pre_review_check'
        _event(request, stage, 'PRE_REVIEW_BLOCKED', codes)
        raise PreReviewFailure(stage, codes)
    resumed = claim_revision(directory, request.id, 'pre_review_correction', identity)
    if not saved['correction_attempted']:
        # Claim durably before any paid call. An interrupted call can only use cache.
        saved.update(correction_attempted=True, issue_codes=codes, changes=changes)
        atomic_json(path, saved)
    _event(request, 'pre_review_correction', 'PRE_REVIEW_CORRECTION_RESUMED' if resumed
           else 'PRE_REVIEW_CORRECTION_STARTED', saved['issue_codes'])
    try:
        corrected = llm.correct_draft(candidate, info, saved['issue_codes'], request,
                                      cache_only=resumed)
    except Exception as exc:
        _event(request, 'pre_review_correction', 'PRE_REVIEW_CORRECTION_FAILED',
               [getattr(exc, 'reason', 'correction_failed')])
        if isinstance(exc, ResponseFailure):
            raise
        raise PreReviewFailure('pre_review_correction', ['correction_failed']) from exc
    saved['corrected'] = asdict(corrected)
    atomic_json(path, saved)
    _event(request, 'pre_review_recheck', 'PRE_REVIEW_RECHECK_STARTED')
    post, codes, final_changes = inspect_candidate(corrected, request, info, require_structure)
    if codes:
        _event(request, 'pre_review_recheck', 'PRE_REVIEW_BLOCKED', codes)
        raise PreReviewFailure('pre_review_recheck', codes)
    report = {'status': 'PASS', 'changes': changes + final_changes,
              'model_correction_used': True}
    atomic_json(path, {**saved, 'report': report})
    _event(request, 'pre_review_recheck', 'PRE_REVIEW_PASSED')
    return post, report
