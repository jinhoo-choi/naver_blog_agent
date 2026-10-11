"""Explicit estimate contracts; no claim of a provider-enforced dollar ceiling."""
from __future__ import annotations

import re
from decimal import Decimal


class CostBoundUnavailable(ValueError):
    pass


def validate_budget(budget: dict, settings) -> None:
    fields = {'writer_model', 'review_model', 'max_calls', 'max_output_tokens',
              'budget_mode', 'max_estimated_usd', 'cost_approval_reference'}
    if not isinstance(budget, dict) or set(budget) != fields:
        raise ValueError('Explicit cost mode and separate owner approval are required')
    if budget['budget_mode'] == 'strict_usd':
        raise CostBoundUnavailable('Hosted search input and Flare image output lack proven hard caps')
    if (budget['budget_mode'] != 'estimated_with_call_caps'
            or type(budget['max_estimated_usd']) not in (int, float)
            or not 0 < budget['max_estimated_usd'] <= 3
            or not isinstance(budget['cost_approval_reference'], str)
            or not re.fullmatch(r'Sentinel_[A-Za-z0-9_-]{8,100}', budget['cost_approval_reference'])
            or budget['writer_model'] != settings.openai_model
            or budget['review_model'] != (settings.review_model or settings.openai_model)
            or {budget['writer_model'], budget['review_model']} != {'gpt-5'}
            or type(budget['max_calls']) is not int or not 2 <= budget['max_calls'] <= 4
            or type(budget['max_output_tokens']) is not int
            or not 18_000 <= budget['max_output_tokens'] <= 36_000):
        raise ValueError('Unsupported explicitly approved estimate/call budget')


def estimate_project(budget: dict, image_prompt: str = '') -> dict:
    # Deliberately identified as a scenario, NOT an upper bound on hosted tool
    # billing. A full context per response is an input allowance assumption;
    # the provider does not promise it bounds all internal search processing.
    text = (Decimal(budget['max_calls']) * Decimal('0.50')
            + Decimal(budget['max_output_tokens']) * Decimal('0.000010')
            + Decimal(budget['max_calls']) * Decimal('0.03'))
    image = (Decimal('0.01317') + Decimal(len(image_prompt.encode())) * Decimal('0.000005')
             if image_prompt else Decimal(0))
    total = text + image
    if total > Decimal(str(budget['max_estimated_usd'])):
        raise ValueError('Preflight project estimate exceeds approved estimated budget')
    return {'mode': 'estimated_with_call_caps', 'not_a_hard_cost_cap': True,
            'estimated_usd': str(total), 'text_scenario_usd': str(text),
            'image_scenario_usd': str(image),
            'input_assumption': '400000 billed input tokens per text response; not a proven bound',
            'image_assumption': '439 output tokens from official medium1024 calculator; estimate only',
            'price_basis': 'OpenAI Standard prices verified 2026-10-10',
            'sources': ['https://developers.openai.com/api/docs/models/gpt-5',
                        'https://developers.openai.com/api/docs/models/gpt-image-2.5-flare',
                        'https://developers.openai.com/api/docs/guides/image-generation',
                        'https://developers.openai.com/api/docs/pricing']}


def usage_cost(response, *, image=False):
    from .responses import _get
    usage = _get(response, 'usage')
    inp, out = _get(usage, 'input_tokens'), _get(usage, 'output_tokens')
    if type(inp) is not int or type(out) is not int or inp < 0 or out < 0:
        return None
    if image:
        # This endpoint sends text only, with no input image or partial streaming.
        return Decimal(inp) * Decimal('0.000005') + Decimal(out) * Decimal('0.000030')
    searches = sum(_get(item, 'type') == 'web_search_call'
                   for item in (_get(response, 'output', []) or []))
    return (Decimal(inp) * Decimal('0.00000125') + Decimal(out) * Decimal('0.000010')
            + Decimal(searches) * Decimal('0.01'))


def thumbnail_spend(settings, request_id):
    import json
    root = settings.db_path.parent / 'manual-thumbnails' / request_id
    if not root.exists():
        return Decimal(0)
    claim = json.loads((root / 'claim.json').read_text())
    if claim.get('status') != 'MANUAL_THUMBNAIL_READY':
        raise ValueError('Unfinished thumbnail must be reconciled before paid prose')
    result = json.loads((root / 'result.json').read_text())
    return Decimal(result['accounted_cost_usd'])
