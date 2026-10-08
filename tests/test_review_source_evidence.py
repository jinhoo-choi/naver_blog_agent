"""Offline source reads and review-cache receipts must not create retroactive approval."""
import hashlib
import io
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from blogbot.core import today_kst
from blogbot.inputs import ContentRequest
from blogbot.llm import _cached_review_request, _checked_review, _review_context
from blogbot.recovery import rejected_checkpoint
from blogbot.research import prepare_reference_evidence
from blogbot.responses import REVIEW_SCHEMA, request_json

SOURCE = 'https://www.nsca.com/education/articles/face-pull/'
LATER_SOURCE = 'https://my.clevelandclinic.org/health/articles/later-evidence'
OFFICIAL_HOSTS = [
    'www.nasm.org', 'www.cdc.gov', 'www.healthychildren.org', 'www.heart.org',
    'www.lullabytrust.org.uk', 'www.acefitness.org', 'www.nsca.com',
    'my.clevelandclinic.org',
]


@pytest.fixture(autouse=True)
def offline(monkeypatch, weekday_clock):
    def unexpected(*args, **kwargs):
        pytest.fail('Unexpected network or live model access in an offline evidence test')

    monkeypatch.setattr('blogbot.research.urlopen', unexpected)
    monkeypatch.setattr('blogbot.llm.OpenAI', unexpected)


@pytest.fixture
def content_request():
    return ContentRequest('source-evidence-test', 'exercise', {'question': '운동 자세 확인'})


def reference(url=SOURCE):
    return {'url': url, 'text': 'Offline original article evidence. ' * 20,
            'retrieved_date': str(today_kst())}


def review(url=SOURCE):
    return {'scores': [5, 5, 5, 5, 5, 5], 'total': 30, 'decision': 'PASS',
            'issues': [], 'blocking_issues': [], 'rewrite_instructions': '',
            'source_checks': [{'claim': '본문의 검증 대상 주장', 'source_url': url,
                               'evidence': '원문 근거', 'status': 'SUPPORTED'}]}


def search_response(url=SOURCE, response_id='review-response'):
    return {'id': response_id, 'status': 'completed', 'output': [
        {'type': 'web_search_call', 'status': 'completed',
         'action': {'type': 'search', 'sources': [{'url': url}]}}]}


def install_html_reader(monkeypatch, calls):
    def read(req, timeout):
        calls.append(req.full_url)
        assert timeout == 25
        stream = io.BytesIO(('<html><body><p>' + reference()['text']
                             + '</p></body></html>').encode())
        stream.url = req.full_url
        return stream

    monkeypatch.setattr('blogbot.research.urlopen', read)


@pytest.mark.parametrize('host', OFFICIAL_HOSTS)
def test_exact_official_hosts_include_two_new_reference_sources(
    tmp_path, monkeypatch, content_request, host,
):
    calls = []
    install_html_reader(monkeypatch, calls)
    url = f'https://{host}/article'
    prepared = prepare_reference_evidence(tmp_path, content_request, [url])
    assert calls == [url]
    evidence, = prepared.provenance['reference_evidence']
    assert evidence['url'] == url and evidence['text']
    assert evidence['sha256'] == hashlib.sha256(evidence['text'].encode()).hexdigest()
    assert evidence['retrieved_date'] == str(today_kst())
    assert content_request.provenance == {}
    assert prepare_reference_evidence(tmp_path, content_request, [url]) == prepared
    assert calls == [url]


@pytest.mark.parametrize('url', [
    'https://www.youtube.com/watch?v=fixture', 'https://youtu.be/fixture',
    'https://unknown.example/article', 'https://nsca.com/article',
    'https://journals.nsca.com/article', 'https://www.nsca.com.evil.example/article',
    'https://clevelandclinic.org/article', 'https://www.clevelandclinic.org/article',
    'https://my.clevelandclinic.org.evil.example/article',
    'https://www.nsca.com@unknown.example/article',
    'http://www.nsca.com/article', 'http://my.clevelandclinic.org/article',
    'https://www.nsca.com/article.pdf', 'https://my.clevelandclinic.org/article.pdf',
])
def test_other_hosts_youtube_http_and_pdf_are_never_fetched(tmp_path, content_request, url):
    prepared = prepare_reference_evidence(tmp_path, content_request, [url])
    assert prepared.provenance['reference_evidence'] == []
    assert list((tmp_path / 'source-evidence').glob('*.json')) == []


@pytest.mark.parametrize('category,directory_present', [
    ('cooking', True), ('investment', True), ('exercise', False),
])
def test_reference_reads_remain_limited_to_eligible_requests(
    tmp_path, content_request, category, directory_present,
):
    content_request = replace(content_request, category=category)
    assert prepare_reference_evidence(
        tmp_path if directory_present else None, content_request, [SOURCE]) is content_request
    assert not (tmp_path / 'source-evidence').exists()


@pytest.mark.parametrize('failed', [False, True])
def test_reference_fetch_cap_counts_three_distinct_eligible_attempts(
    tmp_path, monkeypatch, content_request, failed,
):
    urls = [f'https://{host}/article' for host in OFFICIAL_HOSTS[:4]]
    calls = []
    if failed:
        def read(req, timeout):
            calls.append(req.full_url)
            raise OSError('Offline simulated fetch failure')
        monkeypatch.setattr('blogbot.research.urlopen', read)
    else:
        install_html_reader(monkeypatch, calls)
    prepared = prepare_reference_evidence(
        tmp_path, content_request, ['https://www.youtube.com/watch?v=fixture', urls[0],
                            urls[0], 'https://unknown.example/article', *urls[1:]])
    assert calls == urls[:3]
    assert len(prepared.provenance['reference_evidence']) == (0 if failed else 3)
    assert len(list((tmp_path / 'source-evidence').glob('*.json'))) == 3


def test_failed_reference_fetch_is_cached_without_retry_until_next_date(
    tmp_path, monkeypatch, content_request,
):
    calls = []

    def read(req, timeout):
        calls.append(req.full_url)
        raise OSError('Offline simulated fetch failure')

    monkeypatch.setattr('blogbot.research.urlopen', read)
    first = prepare_reference_evidence(tmp_path, content_request, [SOURCE])
    assert first.provenance['reference_evidence'] == []
    assert prepare_reference_evidence(tmp_path, content_request, [SOURCE]) == first
    assert calls == [SOURCE]
    path, = (tmp_path / 'source-evidence').glob('reference-*.json')
    failed = json.loads(path.read_text())
    assert failed == {'url': SOURCE, 'retrieved_date': str(today_kst()),
                      'text': '', 'error': 'OSError'}
    tomorrow = today_kst() + timedelta(days=1)
    monkeypatch.setattr('blogbot.research.today_kst', lambda: tomorrow)
    prepare_reference_evidence(tmp_path, content_request, [SOURCE])
    assert calls == [SOURCE, SOURCE]


def test_reviewer_receipt_is_persisted_locally_never_sent_or_replaced_on_cache_hit(
    tmp_path, content_request,
):
    content_request = replace(content_request, provenance={'reference_evidence': [reference()]})
    receipt = _review_context(content_request)
    payload, raw = review(), search_response()
    response = NS(status='completed', output_text=json.dumps(payload),
                  id=raw['id'], model='gpt-5', output=raw['output'],
                  model_dump=lambda: raw)
    calls = []

    def create(**params):
        calls.append(params)
        return response

    client = NS(responses=NS(create=create))
    args = {'model': 'gpt-5', 'stage': 'reviewer', 'request_id': content_request.id,
            'schema': REVIEW_SCHEMA, 'journal': tmp_path / 'usage.jsonl',
            'max_output_tokens': 6000, 'input': 'Offline reviewer prompt'}
    assert request_json(client, cache_context=receipt, **args)[0] == payload
    assert len(calls) == 1
    assert 'cache_context' not in calls[0] and 'context' not in calls[0]
    assert reference()['text'] not in json.dumps(calls[0])
    assert 'provenance' not in (tmp_path / 'usage.jsonl').read_text()
    path, = (tmp_path / 'response-cache').glob('*.json')
    assert json.loads(path.read_text()) == {
        'payload': payload, 'response': raw, 'context': receipt}
    later_receipt = _review_context(replace(
        content_request, provenance={'reference_evidence': [reference(LATER_SOURCE)]}))
    assert request_json(client, cache_only=True, cache_context=later_receipt, **args) == (
        payload, raw)
    assert len(calls) == 1
    assert json.loads(path.read_text())['context'] == receipt


def test_review_context_only_records_evidence_and_binds_request_identity(content_request):
    evidence = {'primary_evidence': {'url': SOURCE, 'viewer_url': SOURCE, 'text': 'read'},
                'reference_evidence': [reference()],
                'life_economics_checks': [{'url': SOURCE, 'verified_date': str(today_kst())}]}
    content_request = replace(content_request, provenance={**evidence, 'unrelated': 'not part of receipt'})
    assert _review_context(content_request) == {
        'request_id': content_request.id, 'category': content_request.category, 'provenance': evidence}


@pytest.mark.parametrize('cached', [{}, {'context': {}}, {'context': None}])
def test_legacy_cached_review_cannot_inherit_current_or_later_evidence(content_request, cached):
    evidence = {'primary_evidence': {'url': SOURCE, 'viewer_url': SOURCE, 'text': 'read'},
                'reference_evidence': [reference()],
                'life_economics_checks': [{'url': SOURCE, 'verified_date': str(today_kst())}]}
    content_request = replace(content_request, provenance={**evidence, 'origin': 'preserve metadata'})
    restored = _cached_review_request(content_request, cached)
    assert restored.provenance == {'origin': 'preserve metadata'}
    assert content_request.provenance == {**evidence, 'origin': 'preserve metadata'}
    assert _checked_review(review(), search_response(), restored)['decision'] == 'REWRITE'


def test_matching_receipt_uses_only_evidence_given_to_cached_reviewer(content_request):
    at_review = replace(content_request, provenance={'reference_evidence': [reference()]})
    later = replace(content_request, provenance={'reference_evidence': [reference(LATER_SOURCE)],
                                        'origin': 'keep metadata'})
    restored = _cached_review_request(later, {'context': _review_context(at_review)})
    assert restored.provenance == {**at_review.provenance, 'origin': 'keep metadata'}
    assert _checked_review(review(), search_response(), restored)['decision'] == 'PASS'
    assert _checked_review(
        review(LATER_SOURCE), search_response(LATER_SOURCE), restored)['decision'] == 'REWRITE'


@pytest.mark.parametrize('change', [
    {'request_id': 'different-request'}, {'category': 'parenting'},
])
def test_receipt_identity_mismatch_fails_closed(content_request, change):
    context = {**_review_context(content_request), **change}
    with pytest.raises(ValueError, match='Cached review evidence identity mismatch'):
        _cached_review_request(content_request, {'context': context})


@pytest.mark.parametrize('receipt', [None, {'reference_evidence': []},
                                     {'reference_evidence': [{'url': SOURCE, 'text': ''}]}])
def test_search_results_and_citations_are_not_original_source_reads(content_request, receipt):
    cached = {} if receipt is None else {'context': {
        'request_id': content_request.id, 'category': content_request.category, 'provenance': receipt}}
    response = search_response()
    response['output'].append({'type': 'message', 'content': [
        {'type': 'output_text', 'annotations': [{'type': 'url_citation', 'url': SOURCE}]}]})
    restored = _cached_review_request(content_request, cached)
    checked = _checked_review(review(), response, restored)
    assert checked['decision'] == 'REWRITE'
    assert checked['blocking_issues']


@pytest.mark.parametrize('action', ['open_page', 'find_in_page'])
@pytest.mark.parametrize('status', ['completed', 'in_progress', 'failed'])
def test_legacy_response_needs_its_own_completed_page_read(content_request, action, status):
    response = {'output': [{'type': 'web_search_call', 'status': status,
                            'action': {'type': action, 'url': SOURCE}}]}
    restored = _cached_review_request(content_request, {})
    assert _checked_review(review(), response, restored)['decision'] == (
        'PASS' if status == 'completed' else 'REWRITE')


@pytest.mark.parametrize('later_evidence,has_receipt,expected', [
    (False, False, 'REWRITE'), (True, False, 'REWRITE'), (True, True, 'PASS'),
])
def test_rejected_checkpoint_never_fetches_sources_to_upgrade_a_cached_review(
    tmp_path, monkeypatch, content_request, later_evidence, has_receipt, expected,
):
    def forbidden(*args, **kwargs):
        pytest.fail('Recovery must not fetch references for an already completed review')

    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', forbidden)
    # Also catch a future reintroduction of a direct recovery-module import.
    monkeypatch.setattr('blogbot.recovery.prepare_reference_evidence', forbidden, raising=False)
    if later_evidence:
        content_request = replace(content_request, provenance={'reference_evidence': [reference()]})
    writer = {'payload': {'title': '기존 운동 글', 'subcategory': '운동', 'body': '기존 본문',
                          'tags': [], 'source_urls': [SOURCE], 'as_of_date': str(today_kst())},
              'response': search_response(response_id='writer-response')}
    reviewer = {'payload': review(), 'response': search_response()}
    if has_receipt:
        reviewer['context'] = _review_context(content_request)
    folder = tmp_path / 'response-cache'
    folder.mkdir()
    entries = []
    for stage, cached in [('writer', writer), ('reviewer', reviewer)]:
        (folder / f'{today_kst()}-{stage}.json').write_text(json.dumps(cached))
        entries.append({'request_id': content_request.id, 'stage': stage,
                        'response_id': cached['response']['id'], 'error': None})
    (tmp_path / 'usage.jsonl').write_text('\n'.join(json.dumps(row) for row in entries))
    result = rejected_checkpoint(NS(db_path=tmp_path / 'state.sqlite'), content_request)
    assert result['review']['decision'] == expected
    assert result['post']['body'] == writer['payload']['body']
    assert result['post']['source_urls'] == [SOURCE]
    assert not (tmp_path / 'source-evidence').exists()
    assert json.loads((folder / f'{today_kst()}-reviewer.json').read_text()) == reviewer
