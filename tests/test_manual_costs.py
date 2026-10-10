"""Offline checks that distinguish declared estimates from enforceable call caps."""
import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from blogbot import manual_requests
from blogbot.manual_costs import CostBoundUnavailable, estimate_project, usage_cost, validate_budget


@pytest.fixture
def budget():
    return {'writer_model': 'gpt-5', 'review_model': 'gpt-5', 'max_calls': 4,
            'max_output_tokens': 36000, 'budget_mode': 'estimated_with_call_caps',
            'max_estimated_usd': 3,
            'cost_approval_reference': 'Sentinel_test_only_estimated_cost_consent'}


@pytest.fixture
def settings():
    return SimpleNamespace(openai_model='gpt-5', review_model='gpt-5')


def test_explicit_estimate_is_accepted_as_estimate_only(budget, settings):
    validate_budget(budget, settings)
    result = estimate_project(budget)
    assert result['mode'] == 'estimated_with_call_caps'
    assert result['not_a_hard_cost_cap'] is True
    assert Decimal(result['estimated_usd']) == Decimal('2.48')
    assert Decimal(result['text_scenario_usd']) == Decimal('2.48')
    assert Decimal(result['image_scenario_usd']) == 0
    assert 'not a proven bound' in result['input_assumption']
    assert 'estimate only' in result['image_assumption']


def test_strict_usd_cannot_be_satisfied_by_call_limits_or_approval_string(budget, settings):
    budget['budget_mode'] = 'strict_usd'
    with pytest.raises(CostBoundUnavailable):
        validate_budget(budget, settings)


@pytest.mark.parametrize('model', ['gpt-5-mini', 'gpt-5.1', 'other-model'])
def test_unpriced_models_fail_even_if_they_match_current_configuration(budget, settings, model):
    settings.openai_model = settings.review_model = model
    budget['writer_model'] = budget['review_model'] = model
    with pytest.raises(ValueError):
        validate_budget(budget, settings)


def test_empty_review_setting_uses_existing_writer_model(budget, settings):
    settings.review_model = ''
    validate_budget(budget, settings)


@pytest.mark.parametrize('cap', [3, 3.0, 2.48])
def test_supported_numeric_estimated_cap_covers_scenario(budget, settings, cap):
    budget['max_estimated_usd'] = cap
    validate_budget(budget, settings)
    assert Decimal(estimate_project(budget)['estimated_usd']) <= Decimal(str(cap))


def test_valid_but_insufficient_estimated_cap_stops_project(budget, settings):
    budget['max_estimated_usd'] = 2.47
    validate_budget(budget, settings)
    with pytest.raises(ValueError, match='exceeds approved estimated budget'):
        estimate_project(budget)


def test_lower_text_limits_reduce_estimate_without_claiming_strict_usd(budget):
    budget.update(max_calls=2, max_output_tokens=18000)
    result = estimate_project(budget)
    assert Decimal(result['estimated_usd']) == Decimal('1.24')
    assert result['not_a_hard_cost_cap'] is True


def test_image_scenario_is_included_in_same_project_total_using_utf8_bytes(budget):
    prompt = '공깃밥'
    result = estimate_project(budget, prompt)
    image = Decimal('0.01317') + Decimal(len(prompt.encode())) * Decimal('0.000005')
    assert Decimal(result['image_scenario_usd']) == image
    assert Decimal(result['estimated_usd']) == Decimal(result['text_scenario_usd']) + image
    assert Decimal(result['estimated_usd']) < Decimal(3)
    assert result['not_a_hard_cost_cap'] is True


def test_image_scenario_does_not_get_a_separate_additional_three_dollar_allowance(budget):
    assert Decimal(estimate_project(budget, 'x' * 101366)['estimated_usd']) == Decimal(3)
    with pytest.raises(ValueError, match='exceeds approved estimated budget'):
        estimate_project(budget, 'x' * 101367)


@pytest.mark.parametrize('invalid', [None, [], {}, 'approved'])
def test_absent_or_nonobject_budget_cannot_imply_estimated_cost_consent(settings, invalid):
    with pytest.raises(ValueError):
        validate_budget(invalid, settings)


@pytest.mark.parametrize('usage', [None, {}, {'input_tokens': -1, 'output_tokens': 2},
                                 {'input_tokens': True, 'output_tokens': 2},
                                 {'input_tokens': 1, 'output_tokens': '2'}])
def test_missing_or_malformed_usage_cannot_create_false_precise_cost(usage):
    assert usage_cost({'usage': usage}) is None
    assert usage_cost({'usage': usage}, image=True) is None


def test_reported_text_and_image_usage_apply_distinct_declared_prices():
    response = {'usage': {'input_tokens': 1000, 'output_tokens': 439},
                'output': [{'type': 'web_search_call'}, {'type': 'message'},
                           {'type': 'web_search_call'}]}
    assert usage_cost(response) == Decimal('0.02564')
    assert usage_cost(response, image=True) == Decimal('0.01817')


def test_initial_image_spend_reduces_remaining_text_allowance(budget, tmp_path):
    submitted = []
    transport = SimpleNamespace(create=lambda **kw: submitted.append(kw))
    path = tmp_path / 'paid-calls.json'
    adapter = manual_requests.BudgetedResponses(transport, path, budget,
                                               initial_spend=Decimal('2.50'))
    with pytest.raises(ValueError, match='remaining approved estimated budget'):
        adapter.create(model='gpt-5', max_output_tokens=12000)
    assert submitted == []
    assert not path.exists()


def test_reported_spend_stops_next_call_even_when_count_and_tokens_remain(budget, tmp_path):
    submitted = []

    def create(**kwargs):
        submitted.append(kwargs)
        return {'usage': {'input_tokens': 2400000, 'output_tokens': 0}, 'output': []}

    path = tmp_path / 'paid-calls.json'
    adapter = manual_requests.BudgetedResponses(SimpleNamespace(create=create), path, budget)
    adapter.create(model='gpt-5', max_output_tokens=12000, service_tier='priority')
    assert submitted[0]['service_tier'] == 'default'
    stored = json.loads(path.read_text())
    assert stored[0]['cost_basis'] == 'reported_usage'
    assert Decimal(stored[0]['accounted_cost_usd']) == Decimal(3)
    with pytest.raises(ValueError, match='remaining approved estimated budget'):
        adapter.create(model='gpt-5', max_output_tokens=6000)
    assert len(submitted) == 1
    assert json.loads(path.read_text()) == stored


def test_missing_usage_retains_preflight_cost_reservation(budget, tmp_path):
    path = tmp_path / 'paid-calls.json'
    transport = SimpleNamespace(create=lambda **kw: SimpleNamespace(usage=None))
    manual_requests.BudgetedResponses(transport, path, budget).create(
        model='gpt-5', max_output_tokens=12000)
    call = json.loads(path.read_text())[0]
    assert call['cost_basis'] == 'preflight_scenario'
    assert Decimal(call['accounted_cost_usd']) == Decimal('0.65')


def test_midnight_blocks_next_text_submission_without_rewriting_spend_journal(
        budget, tmp_path, monkeypatch):
    day = [date(2026, 10, 10)]
    monkeypatch.setattr(manual_requests, 'today_kst', lambda: day[0])
    submitted = []
    transport = SimpleNamespace(create=lambda **kw: submitted.append(kw))
    path = tmp_path / 'paid-calls.json'
    adapter = manual_requests.BudgetedResponses(transport, path, budget, approved_date='2026-10-10')
    adapter.create(model='gpt-5', max_output_tokens=12000)
    stored = path.read_bytes()
    assert json.loads(stored)[0]['actual_call_date_kst'] == '2026-10-10'
    day[0] = date(2026, 10, 11)
    with pytest.raises(ValueError, match='crossed midnight'):
        adapter.create(model='gpt-5', max_output_tokens=6000)
    assert len(submitted) == 1
    assert path.read_bytes() == stored
