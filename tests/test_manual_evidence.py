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


def test_actual_origins_review_prompt_receives_and_uses_verified_excerpts(monkeypatch):
    from pathlib import Path

    from blogbot import llm
    from blogbot.core import PostDraft
    from blogbot.llm import BlogLLM
    url = 'https://www.korean.go.kr/front/onlineQna/onlineQnaView.do?qna_seq=314641'
    evidence = approved_source_evidence({'sources': [{'url': url, 'excerpt': '공기는 그릇이다.'}],
                                        'approval_reference': 'Sentinel_test_owner', 'date': '2026-10-11'})
    request = ContentRequest('extra', 'origins', {'question': '공기 뜻은?', 'sources': [
        {'url': url, 'excerpt': '공기는 그릇이다.'}]}, [], {'reference_evidence': evidence})
    post = PostDraft('origins', '음식·생활', '공기', '공기는?', '공기는 그릇입니다.', [], [url], '2026-10-11')
    calls = []
    def respond(*args, **kwargs):
        calls.append(kwargs)
        return {'decision': 'PASS', 'source_checks': [{'source_url': url,
            'status': 'SUPPORTED', 'evidence': '공기는 그릇이다.'}]}, {'output': []}
    monkeypatch.setattr(llm, 'request_json', respond)
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)
    client = BlogLLM.__new__(BlogLLM)
    client.client = client.journal = None
    client.model = client.review_model = 'offline'
    client.reviewer_prompt = (Path(__file__).resolve().parents[1] / 'prompts/reviewer.md').read_text()
    result = client.review(post, {'rules': []}, request)
    assert result['decision'] == 'PASS'
    prompt = calls[0]['input']
    assert '이미 제공된 범위는 재검색하지 않는다' in prompt
    assert '부족하면 최대3개 주요 원문을 실제 열어 확인한다' in prompt
    assert '검색 결과 요약·URL 목록만으로 승인하지 않는다' in prompt
    assert '본문이 제공되지 않은 출처(핵심 주장에 쓰인 URL부터 직접 열기):\n[]' in prompt
    assert evidence[0]['sha256'] in prompt and evidence[0]['text'] in prompt
    assert calls[0]['cache_context']['provenance']['reference_evidence'] == evidence
    assert calls[0]['max_output_tokens'] == 6000
    assert calls[0]['max_tool_calls'] == 3
    assert calls[0]['tool_choice'] == 'auto'
    # Without supplied originals, keep the required direct-research path.
    request.provenance['reference_evidence'] = []
    held = client.review(post, {'rules': []}, request)
    assert calls[-1]['tool_choice'] == 'required'
    assert held['decision'] == 'REWRITE'


def test_origins_missing_or_altered_supplied_evidence_still_fails_closed():
    import copy

    import pytest
    url = 'https://www.korean.go.kr/front/onlineQna/onlineQnaView.do?qna_seq=314641'
    valid = approved_source_evidence({'sources': [{'url': url, 'excerpt': '공기는 그릇이다.'}],
                                     'approval_reference': 'Sentinel_test_owner', 'date': '2026-10-11'})
    for field in ('url', 'text', 'sha256', 'verified_by'):
        evidence = copy.deepcopy(valid)
        evidence[0].pop(field)
        request = ContentRequest('extra', 'origins', {'question': '공기 뜻은?'}, [],
                                 {'reference_evidence': evidence})
        with pytest.raises(ValueError, match='incomplete or altered'):
            _checked_review({'source_checks': []}, {'output': []}, request)
    evidence = copy.deepcopy(valid)
    evidence[0]['text'] = '해시와 다른 내용'
    request = ContentRequest('extra', 'origins', {'question': '공기 뜻은?'}, [],
                             {'reference_evidence': evidence})
    with pytest.raises(ValueError, match='incomplete or altered'):
        _checked_review({'source_checks': []}, {'output': []}, request)
    request.provenance['reference_evidence'] = []
    payload = {'decision': 'PASS', 'source_checks': [{'source_url': url, 'status': 'SUPPORTED', 'evidence': '뜻'}]}
    assert _checked_review(payload, {'output': []}, request)['decision'] == 'REWRITE'
