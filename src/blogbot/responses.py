"""Bounded structured Responses calls with private, content-free usage diagnostics."""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path


class ResponseFailure(RuntimeError):
    def __init__(self, stage: str, reason: str):
        self.stage, self.reason = stage, reason
        super().__init__(f'{stage}: {reason}')


def object_schema(properties: dict) -> dict:
    return {'type': 'object', 'properties': properties, 'required': list(properties),
            'additionalProperties': False}


STRING = {'type': 'string'}
STRINGS = {'type': 'array', 'items': STRING}
DRAFT_SCHEMA = object_schema({
    'title': STRING, 'subcategory': STRING, 'body': STRING, 'tags': STRINGS,
    'source_urls': STRINGS, 'as_of_date': STRING,
})
REVIEW_SCHEMA = object_schema({
    'scores': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1, 'maximum': 5},
               'minItems': 6, 'maxItems': 6},
    'total': {'type': 'integer'}, 'decision': {'type': 'string', 'enum': ['PASS', 'REWRITE', 'DROP']},
    'issues': STRINGS, 'blocking_issues': STRINGS, 'rewrite_instructions': STRING,
    'source_checks': {'type': 'array', 'items': object_schema({
        'claim': STRING, 'source_url': STRING, 'evidence': STRING,
        'status': {'type': 'string', 'enum': ['SUPPORTED', 'CONTRADICTED', 'UNVERIFIED']},
    })},
})
BENCHMARK_SCHEMA = object_schema({
    'records': {'type': 'array', 'items': object_schema({'url': STRING, 'observations': STRINGS})},
    'limitations': STRING,
})


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def record_usage(path: Path, *, response=None, stage: str, request_id: str, model: str,
                 attempt: int, elapsed: float, error: str | None = None) -> None:
    """Never persist prompt, response text, refusal text, credentials or API error messages."""
    usage = _get(response, 'usage')
    tokens = {name: _get(usage, name) for name in ['input_tokens', 'output_tokens', 'total_tokens']}
    tokens['cached_input_tokens'] = _get(_get(usage, 'input_tokens_details'), 'cached_tokens')
    tokens['reasoning_tokens'] = _get(_get(usage, 'output_tokens_details'), 'reasoning_tokens')
    output = _get(response, 'output', []) or []
    searches = sum(_get(item, 'type') == 'web_search_call' for item in output)
    row = {
        'timestamp_utc': datetime.now(UTC).isoformat(), 'stage': stage, 'request_id': request_id,
        'model': _get(response, 'model', model), 'attempt': attempt,
        'response_id': _get(response, 'id'), 'status': _get(response, 'status'),
        'incomplete_reason': _get(_get(response, 'incomplete_details'), 'reason'),
        'elapsed_seconds': round(elapsed, 3), 'tokens': tokens,
        'web_search_calls': searches, 'error': error,
    }
    # Estimate only known standard GPT-5 text rates. Search content reconciliation and
    # tier/discount/tax differences remain the billing dashboard's responsibility.
    actual_model = row['model'] or ''
    tier = _get(response, 'service_tier')
    if (actual_model == 'gpt-5' or actual_model.startswith('gpt-5-20')) and tier in (None, 'default'):
        inp, out, cached = tokens['input_tokens'], tokens['output_tokens'], tokens['cached_input_tokens']
        if isinstance(inp, int) and isinstance(out, int):
            cached = cached if isinstance(cached, int) else 0
            row['estimated_text_usd'] = round(((inp-cached)*1.25+cached*0.125+out*10)/1_000_000, 8)
            row['estimated_search_call_usd'] = round(searches*0.01, 8)
            row['estimate_basis'] = '2026-09-28 standard rates; not a billing total'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(row, ensure_ascii=False)+'\n')
        stream.flush()
        os.fsync(stream.fileno())


def _payload(response, stage: str) -> dict:
    for item in _get(response, 'output', []) or []:
        for part in _get(item, 'content', []) or []:
            if _get(part, 'type') == 'refusal':
                raise ResponseFailure(stage, 'refusal')
    if _get(response, 'status') != 'completed':
        reason = _get(_get(response, 'incomplete_details'), 'reason')
        raise ResponseFailure(stage, 'max_output_tokens' if reason == 'max_output_tokens'
                              else 'response_not_completed')
    text = _get(response, 'output_text', '')
    if not isinstance(text, str) or not text.strip():
        raise ResponseFailure(stage, 'empty_output')
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise ResponseFailure(stage, 'invalid_json') from None
    if not isinstance(value, dict):
        raise ResponseFailure(stage, 'non_object_json')
    return value


def request_json(client, *, model: str, stage: str, request_id: str, schema: dict,
                 journal: Path, max_output_tokens: int, retry_output_tokens: int | None = None,
                 cache_only: bool = False, validate_required: bool = True, **kwargs):
    """One retry only for an explicitly truncated response; no timeout blind retry."""
    cache = None
    if stage in {'benchmark', 'writer', 'reviewer', 'rewrite', 'pre_review_correction'}:
        from .core import today_kst
        from .images import atomic_json
        identity = json.dumps([model, stage, request_id, kwargs, schema], sort_keys=True)
        cache = journal.parent / 'response-cache' / (
            today_kst().isoformat() + '-' + hashlib.sha256(identity.encode()).hexdigest() + '.json')
        cache.parent.mkdir(parents=True, exist_ok=True)
        if cache.exists():
            saved = json.loads(cache.read_text(encoding='utf-8'))
            return saved['payload'], saved['response']
    if cache_only:
        raise ResponseFailure(stage, 'cached_response_unavailable')
    budgets = [max_output_tokens]
    if retry_output_tokens and retry_output_tokens > max_output_tokens:
        budgets.append(retry_output_tokens)
    for attempt, budget in enumerate(budgets, 1):
        started = time.monotonic()
        response = None
        error = None
        try:
            response = client.responses.create(
                model=model, store=False, max_output_tokens=budget,
                prompt_cache_key=f'naver-blog-agent:{stage}',
                text={'format': {'type': 'json_schema', 'name': stage, 'strict': True,
                                 'schema': schema}},
                **kwargs,
            )
            payload = _payload(response, stage)
            missing = set(schema['required'])-set(payload)
            if missing and validate_required:
                raise ResponseFailure(stage, 'missing_fields')
            if cache is not None and hasattr(response, 'model_dump'):
                atomic_json(cache, {'payload': payload, 'response': response.model_dump()})
            return payload, response
        except ResponseFailure as exc:
            error = exc.reason
            if exc.reason == 'max_output_tokens' and attempt < len(budgets):
                continue
            raise
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            record_usage(journal, response=response, stage=stage, request_id=request_id,
                         model=model, attempt=attempt, elapsed=time.monotonic()-started, error=error)
    raise AssertionError('unreachable')
