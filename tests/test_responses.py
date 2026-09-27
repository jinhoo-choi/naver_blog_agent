import json
from types import SimpleNamespace as NS

import pytest

from blogbot.responses import BENCHMARK_SCHEMA, ResponseFailure, request_json


def response(status='completed', text='{"records":[],"limitations":"none"}', reason=None):
    return NS(status=status, output_text=text, id='resp_test', model='gpt-5',
              incomplete_details=NS(reason=reason), output=[],
              usage=NS(input_tokens=100, output_tokens=20, total_tokens=120,
                       input_tokens_details=NS(cached_tokens=10),
                       output_tokens_details=NS(reasoning_tokens=5)))


def call(tmp_path, outputs, **kwargs):
    calls = []
    def create(**params):
        calls.append(params)
        item = outputs.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    client = NS(responses=NS(create=create))
    args = {'model':'gpt-5', 'stage':'benchmark', 'request_id':'test', 'schema':BENCHMARK_SCHEMA,
            'journal':tmp_path/'usage.jsonl', 'max_output_tokens':6000, 'retry_output_tokens':10000,
            'input':'PRIVATE INPUT NEVER LOG THIS', 'reasoning':{'effort':'low'}}
    args.update(kwargs)
    return client, calls, args


def test_truncation_retries_once_and_records_both_charges(tmp_path):
    client, calls, args = call(tmp_path, [response('incomplete', '', 'max_output_tokens'), response()])
    payload, _ = request_json(client, **args)
    assert payload['records'] == []
    assert [c['max_output_tokens'] for c in calls] == [6000, 10000]
    assert all(c['text']['format']['strict'] and not c['store'] for c in calls)
    rows = [json.loads(l) for l in (tmp_path/'usage.jsonl').read_text().splitlines()]
    assert len(rows) == 2 and rows[0]['error'] == 'max_output_tokens'
    assert rows[0]['tokens']['output_tokens'] == 20
    assert 'PRIVATE INPUT' not in (tmp_path/'usage.jsonl').read_text()


@pytest.mark.parametrize('item,reason', [
    (response(text=''), 'empty_output'), (response(text='{'), 'invalid_json'),
    (response(text='[]'), 'non_object_json'), (response(text='{}'), 'missing_fields'),
    (response('incomplete', '', 'content_filter'), 'response_not_completed'),
])
def test_invalid_output_stops_without_paid_retry(tmp_path, item, reason):
    client, calls, args = call(tmp_path, [item])
    with pytest.raises(ResponseFailure, match=reason):
        request_json(client, **args)
    assert len(calls) == 1
    assert json.loads((tmp_path/'usage.jsonl').read_text())['error'] == reason


def test_refusal_is_not_retried_or_logged_verbatim(tmp_path):
    item = response()
    item.output = [NS(content=[NS(type='refusal', refusal='PRIVATE REFUSAL')])]
    client, calls, args = call(tmp_path, [item])
    with pytest.raises(ResponseFailure, match='refusal'):
        request_json(client, **args)
    assert len(calls) == 1 and 'PRIVATE REFUSAL' not in (tmp_path/'usage.jsonl').read_text()


def test_timeout_is_uncertain_no_blind_retry(tmp_path):
    client, calls, args = call(tmp_path, [TimeoutError('PRIVATE API ERROR')])
    with pytest.raises(TimeoutError):
        request_json(client, **args)
    assert len(calls) == 1
    row = json.loads((tmp_path/'usage.jsonl').read_text())
    assert row['error'] == 'TimeoutError' and row['tokens']['input_tokens'] is None
    assert 'estimated_text_usd' not in row and 'PRIVATE API ERROR' not in json.dumps(row)


def test_repeated_truncation_has_a_hard_cap(tmp_path):
    client, calls, args = call(tmp_path, [response('incomplete', '', 'max_output_tokens')]*2)
    with pytest.raises(ResponseFailure, match='max_output_tokens'):
        request_json(client, **args)
    assert len(calls) == 2
