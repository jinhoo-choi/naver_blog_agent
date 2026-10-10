import hashlib

from blogbot.inputs import ContentRequest
from blogbot.llm import _cached_review_request, _checked_review, _review_context
from blogbot.manual_requests import approved_source_evidence


def test_approved_excerpts_are_attributed_supplied_input_not_model_browsing():
    url = 'https://www.korean.go.kr/front/onlineQna/onlineQnaView.do?qna_seq=314641'
    packet = {'sources': [{'url': url, 'excerpt': '공기와 밥이 결합한 합성어입니다.'}],
              'approval_reference': 'Sentinel_explicit_owner', 'date': '2026-10-11'}
    evidence = approved_source_evidence(packet)
    assert evidence[0]['text'] == packet['sources'][0]['excerpt']
    assert evidence[0]['sha256'] == hashlib.sha256(evidence[0]['text'].encode()).hexdigest()
    assert evidence[0]['verified_by'] == 'operator_verified_owner_approved_packet'
    assert 'retrieved_date' not in evidence[0]
    request = ContentRequest('extra', 'origins', {'question': '공깃밥은?', 'sources': packet['sources']},
                             [], {'reference_evidence': evidence})
    assert request.prompt_data()['provenance']['reference_evidence'] == evidence
    context = _review_context(request)
    assert context['provenance']['reference_evidence'] == evidence
    payload = {'decision': 'PASS', 'source_checks': [{'source_url': url,
               'status': 'SUPPORTED', 'evidence': evidence[0]['text']}]}
    assert _checked_review(payload, {'output': []}, request)['decision'] == 'PASS'
    # A prior response without the supplied-input receipt must remain held.
    historical = _cached_review_request(request, {'context': {'request_id': 'extra',
                                         'category': 'origins', 'provenance': {}}})
    assert _checked_review(payload, {'output': []}, historical)['decision'] == 'REWRITE'
