"""Short-form origins and future commerce boundaries; fully offline."""
import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from blogbot.config import load_settings
from blogbot.core import PostDraft, connect_db, validate_post
from blogbot.editorial import quality_guidance, routed_prompt
from blogbot.images import ImagePending, generate_images
from blogbot.inputs import ContentRequest, collect_requests, enqueue_file
from blogbot.origins import DISCLOSURE, SERIES_MOTIVE, validate_origin_data, validate_origin_post
from blogbot.pipeline import run_daily, save_pending
from blogbot.planning import resolve_plan, saved_count
from blogbot.pre_review import DraftCandidate, inspect_candidate
from blogbot.presentation import render_segments, validate_structure

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://stdict.korean.go.kr/search/searchView.do?word_no=123'


def request(**data):
    return ContentRequest('origins-question', 'origins', {'question': '실비는 무슨 뜻인가요?', **data})


def post(**data):
    req = request(**data)
    return PostDraft('origins', '음식·생활', '실비', '실비김치의 실비는 무슨 뜻일까요?',
                     '**실비**는 실제로 드는 비용을 뜻합니다.\n\n확인한 사전의 뜻을 설명합니다.',
                     ['실비김치', '이름의유래'], [SOURCE], '2026-10-13', request_id=req.id,
                     provenance=req.provenance)


def settings(tmp_path, monkeypatch, day='2026-10-13'):
    current = date.fromisoformat(day)
    for module in ['core', 'config', 'planning', 'inputs', 'llm']:
        monkeypatch.setattr(f'blogbot.{module}.today_kst', lambda: current)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    return load_settings()


def purchase():
    return {'owner_confirmed': True, 'publication_approved': True,
            'product': '사용자가 산 상품', 'experience': '사용자가 제공하고 공개를 승인한 경험',
            'origin_relevance': '실제로 구매한 상품 이름의 뜻을 묻는 질문입니다.'}


def affiliate():
    return {'provider': 'naver_shopping_connect', 'product': '사용자가 산 상품',
            'product_url': 'https://smartstore.naver.com/actual/products/123',
            'destination_url': 'https://naver.me/actualApprovedLink',
            'owner_approved': True, 'eligibility_verified': True, 'disclosure': DISCLOSURE}


def thumbnail(tmp_path):
    file = tmp_path / 'approved.png'
    file.write_bytes(b'provided image bytes')
    return {'file': str(file), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
            'caption': '', 'role': 'thumbnail', 'approved': True, 'generated': True}


def test_new_plan_preserves_old_date_and_short_depth(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    old = resolve_plan(s.config, date(2026, 10, 5))
    assert old['version'] == 'daily-deep-511-v1' and old['category'] == 'parenting'
    new = resolve_plan(s.config, date(2026, 10, 6))
    assert new['version'] == 'weekly-4111-v1' and new['category'] == 'origins'
    assert new['depth'] == 'short' and new['target'] == 1
    assert s.config['categories']['origins']['display_name'] == '이름의 유래'
    assert not any('Work 임시저장 전 확인 가능한 실제 이미지2장' in rule
                   for rule in s.config['categories']['origins']['rules'])


@pytest.mark.parametrize('day,category', [('2026-10-06', 'origins'),
                                        ('2026-10-07', 'parenting'),
                                        ('2026-10-09', 'investment')])
def test_manual_pending_slots_do_not_claim_save_or_buy_calls(tmp_path, monkeypatch, day, category):
    s = settings(tmp_path, monkeypatch, day)
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: pytest.fail('No new input'))
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', lambda *a: pytest.fail('No paid model'))
    result = run_daily(s)[0]
    assert result['status'] == 'EDITORIAL_SLOT_RESERVED' and result['category'] == category
    assert result['reason'] in {'owner_preparation_pending', 'existing_owner_draft'}
    with connect_db(s.db_path) as conn:
        assert saved_count(conn, s.config['daily_plan']) == 0
        assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 0


def test_unverified_category_blocks_before_paid_calls(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    s.config['categories']['origins'].pop('naver_category_no', None)
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: pytest.fail('No selection'))
    monkeypatch.setattr('blogbot.pipeline.make_writer', lambda _: pytest.fail('No browser'))
    assert run_daily(s) == save_pending(s) == [
        {'status': 'CATEGORY_CONFIGURATION_PENDING', 'category': 'origins'}]


def test_short_source_checked_article_passes_without_length_padding(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    draft = post()
    validate_structure(draft)
    validate_post(draft, s.config['categories']['origins'])
    payload = {k: getattr(draft, k) for k in
               ['title', 'subcategory', 'body', 'tags', 'source_urls', 'as_of_date']}
    checked, codes, _ = inspect_candidate(DraftCandidate(payload, [SOURCE]), request(),
                                          s.config['categories']['origins'])
    assert checked and codes == []
    with pytest.raises(ValueError, match='Images are placed'):
        validate_structure(replace(draft, body=draft.body + '\n\n<img src="fake">'))
    _, codes, _ = inspect_candidate(DraftCandidate(payload, []), request(),
                                    s.config['categories']['origins'])
    assert 'no_observed_evidence' in codes


def test_all_origins_model_paths_keep_short_contract_and_standard_review(monkeypatch):
    from blogbot import llm
    from blogbot.llm import BlogLLM
    calls = []
    draft = post()
    payload = {k: getattr(draft, k) for k in
               ['title', 'subcategory', 'body', 'tags', 'source_urls', 'as_of_date']}
    review = {'scores': [4]*6, 'total': 24, 'decision': 'PASS', 'issues': [],
              'blocking_issues': [], 'rewrite_instructions': '', 'source_checks': [
                  {'claim': '뜻', 'source_url': SOURCE, 'evidence': '원문', 'status': 'SUPPORTED'}]}
    def respond(*args, **kwargs):
        calls.append(kwargs)
        return (review if kwargs['stage'] == 'reviewer' else payload), {
            'output': [{'status': 'completed', 'action': {'type': 'open_page', 'url': SOURCE}}]}
    monkeypatch.setattr(llm, 'request_json', respond)
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)
    client = BlogLLM.__new__(BlogLLM)
    client.client = client.journal = None
    client.model = client.review_model = 'offline'
    client.writer_prompt = (ROOT/'prompts/writer.md').read_text()
    client.reviewer_prompt = (ROOT/'prompts/reviewer.md').read_text()
    info = {'display_name': '이름의 유래', 'subcategories': ['음식·생활'], 'rules': []}
    client.create_draft(request(), info, [])
    client.review(draft, info, request())
    client.rewrite(draft, info, review, request())
    client.correct_draft(DraftCandidate(payload, [SOURCE]), info, [], request())
    assert [c['stage'] for c in calls] == ['writer','reviewer','rewrite','pre_review_correction']
    for call in calls:
        text = call['input']
        assert 'weekly-4111-v1' in text and 'origins-short-v1' in text
        assert '350~600' in text and '1,800자' not in text and '최소 1800자' not in text
        assert '대제목 ## 최소 4개' not in text and '실제 이미지2장 이상' not in text
    assert '총점 30점 중 24점 이상 PASS' in calls[1]['input']
    assert '정확성 또는 안전규칙 점수가 4 미만이면' in calls[1]['input']
    assert all(c['max_tool_calls'] == 3 for c in calls[:3])
    assert 'tools' not in calls[-1]


def test_approved_thumbnail_never_calls_image_api_or_forces_sections(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    monkeypatch.setattr('blogbot.images.OpenAI', lambda **k: pytest.fail('No paid images'))
    req = request()
    req.photos = [thumbnail(tmp_path)]
    result = generate_images(s, req, post())
    assert result.photos == req.photos and len(result.photos) == 1
    with pytest.raises(ImagePending):
        generate_images(s, request(), post())
    req.photos[0]['approved'] = False
    with pytest.raises(ValueError, match='approved'):
        generate_images(s, req, post())


def test_origins_enqueue_retains_thumbnail_and_only_current_topic(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    thumb = thumbnail(tmp_path)
    source = tmp_path/'request.json'
    source.write_text(json.dumps({'id': 'origins-question', 'category': 'origins',
                                 'question': '실비는 무슨 뜻인가요?', 'photos': [thumb]}))
    enqueue_file(s, source)
    requests, notices = collect_requests(s)
    assert len(requests) == 1 and not notices
    assert requests[0].photos[0]['approved'] is True
    assert Path(requests[0].photos[0]['file']).is_relative_to(s.inbox_dir)
    assert requests[0].provenance['origins'] == validate_origin_data({})
    assert quality_guidance('origins') in routed_prompt('', 'origins') + quality_guidance('origins')


def test_origins_skips_unrelated_five_blog_benchmark(tmp_path, monkeypatch):
    from blogbot.research import prepare_request
    s = settings(tmp_path, monkeypatch)
    monkeypatch.setattr('blogbot.research.public_query', lambda _: pytest.fail('No paid benchmark'))
    req = request()
    assert prepare_request(s, req) is req


@pytest.mark.parametrize('change', [
    lambda data: data.update(purchase=None),
    lambda data: data['purchase'].update(owner_confirmed=False),
    lambda data: data['purchase'].update(publication_approved=False),
    lambda data: data['purchase'].update(experience=''),
    lambda data: data['purchase'].update(origin_relevance=''),
    lambda data: data['affiliate'].update(owner_approved=False),
    lambda data: data['affiliate'].update(eligibility_verified=False),
    lambda data: data['affiliate'].update(product='다른 상품'),
    lambda data: data['affiliate'].update(disclosure=''),
    lambda data: data['affiliate'].update(destination_url='javascript:alert(1)'),
    lambda data: data['affiliate'].update(destination_url='https://user:pass@site.test/'),
    lambda data: data['affiliate'].update(new_link_request=True),
])
def test_affiliate_requires_explicit_product_purchase_destination_disclosure(change):
    data = {'purchase': purchase(), 'affiliate': affiliate()}
    change(data)
    with pytest.raises(ValueError):
        validate_origin_data(data)


@pytest.mark.parametrize('body', ['제가 직접 구매했습니다.', '내돈내산 김치입니다.',
                                   '쇼핑커넥트 링크를 눌러 보세요.',
                                   '뜻입니다. https://naver.me/unapproved'])
def test_educational_origins_do_not_invent_commerce(body):
    with pytest.raises(ValueError):
        validate_origin_post(replace(post(), body=body))


def test_affiliate_renders_disclosure_first_and_exact_link_only():
    draft = post(purchase=purchase(), affiliate=affiliate())
    html = ''.join(s.html for s in render_segments(draft))
    assert html.index(DISCLOSURE) < html.index(SERIES_MOTIVE) < html.index('실비')
    assert html.count(affiliate()['destination_url']) == 1
    with pytest.raises(ValueError, match='ad-free'):
        validate_origin_post(replace(draft, body=draft.body + '\n\n광고 없습니다.'))
    plain = ''.join(s.html for s in render_segments(post()))
    assert DISCLOSURE not in plain and '내돈내산' not in plain
    assert plain.count(SERIES_MOTIVE) == 1


def test_pending_notification_does_not_claim_saved_draft(tmp_path, monkeypatch):
    from blogbot.notify import telegram
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(tmp_path/'summary.txt'))
    monkeypatch.setenv('BLOG_NOTIFY_ENABLED', 'false')
    telegram([{'status': 'EDITORIAL_SLOT_RESERVED', 'reason': 'owner_preparation_pending'}])
    report = (tmp_path/'summary.txt').read_text()
    assert '저장 확인 아님' in report and '기존 수동 초안' not in report


def test_missing_thumbnail_is_blocked_before_any_paid_preparation(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    raw = {'id': 'no-thumb', 'category': 'origins', 'question': '실비 뜻은 무엇인가요?'}
    source = tmp_path/'no-thumb.json'
    source.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='before paid preparation'):
        enqueue_file(s, source)
    # Restored/legacy queue manifests are checked independently.
    folder = s.inbox_dir/'no-thumb'
    folder.mkdir(parents=True)
    (folder/'request.json').write_text(json.dumps({
        'id': 'no-thumb', 'category': 'origins', 'data': {'question': raw['question']}, 'photos': []}))
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', lambda *a, **k: pytest.fail('No paid text'))
    assert run_daily(s) == [{'status': 'INPUT_REJECTED'}]
    with connect_db(s.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


def test_restored_origin_queue_rechecks_aggregate_photo_budget(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    source = tmp_path/'request.json'
    source.write_text(json.dumps({'id': 'origins-question', 'category': 'origins',
                                 'question': '실비 뜻이 궁금합니다.', 'photos': [thumbnail(tmp_path)]}))
    enqueue_file(s, source)
    monkeypatch.setattr('blogbot.inputs.MAX_MANAGED_PHOTO_BYTES', 1)
    requests, notices = collect_requests(s)
    assert not requests and notices == [{'status': 'INPUT_REJECTED'}]


def test_nonmanaged_origin_thumbnail_name_is_rejected_before_paid_work(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    folder = s.inbox_dir/'origins-question'
    folder.mkdir(parents=True)
    photo = thumbnail(folder)  # Real file but not the managed photo-01.png name.
    (folder/'request.json').write_text(json.dumps({
        'id': 'origins-question', 'category': 'origins', 'data': {'question': '실비 뜻은?'},
        'photos': [photo]}))
    assert collect_requests(s) == ([], [{'status': 'INPUT_REJECTED'}])


def test_origin_thumbnail_and_metadata_survive_encrypted_transport(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    from blogbot.cloud import extract_bundle, pack
    from blogbot.core import load_post, save_post
    s = settings(tmp_path/'state', monkeypatch)
    monkeypatch.setattr('blogbot.cloud.today_kst', lambda: date(2026, 10, 13))
    source = tmp_path/'request.json'
    source.write_text(json.dumps({'id': 'origins-question', 'category': 'origins',
                                 'question': '실비 뜻은?', 'photos': [thumbnail(tmp_path)]}))
    enqueue_file(s, source)
    req = collect_requests(s)[0][0]
    draft = replace(post(), status='APPROVED', photos=req.photos,
                    provenance={**req.provenance, 'daily_plan': s.config['daily_plan']})
    s.artifact_dir.mkdir(parents=True)
    with connect_db(s.db_path) as conn:
        ident = save_post(conn, draft)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    destination = tmp_path/'bundle.enc'
    pack(s, destination)
    restored = tmp_path/'restored'
    extract_bundle(destination.read_bytes(), restored, key)
    ready = json.loads((restored/'ready.json').read_text())['posts'][0]
    assert ready['category_no'] == 9
    assert ready['post']['provenance']['origins'] == validate_origin_data({})
    assert Path(ready['post']['photos'][0]['file']).read_bytes() == b'provided image bytes'
    with connect_db(restored/'blog.db') as conn:
        recovered = load_post(conn.execute('SELECT * FROM posts WHERE id=?', (ident,)).fetchone())
    assert recovered.photos[0]['approved'] is True
    assert Path(recovered.photos[0]['file']).is_relative_to(restored)
    assert SERIES_MOTIVE in ''.join(s.html for s in render_segments(recovered))


def test_scheduled_origin_cannot_bypass_private_thumbnail_registration(tmp_path, monkeypatch):
    s = settings(tmp_path, monkeypatch)
    s.config['topics']['scheduled']['2026-10-13'] = {
        'id': 'scheduled-origin', 'category': 'origins', 'data': {'question': '실비 뜻은?'}}
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', lambda *a, **k: pytest.fail('No paid text'))
    with pytest.raises(ValueError, match='private input queue'):
        run_daily(s)
    with connect_db(s.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


def test_silbi_editorial_date_is_separate_from_observed_save_day(tmp_path, monkeypatch):
    from blogbot.cloud import import_manual_saves
    from blogbot.planning import receipt_day
    s = settings(tmp_path, monkeypatch, '2026-10-06')
    receipt = next(r for r in json.loads((ROOT/'config/manual-saves.json').read_text())['records']
                   if r['request_id'] == 'manual-origins-silbi-20261005')
    assert receipt_day(receipt) == '2026-10-05'
    assert 'saved_display_timezone' not in receipt
    assert s.config['daily_plan']['reservation']['kind'] == 'existing_owner_draft'
    import_manual_saves(s)
    with connect_db(s.db_path) as conn:
        assert saved_count(conn, s.config['daily_plan']) == 0
        saved = conn.execute('SELECT day FROM save_receipts WHERE request_id=?',
                             (receipt['request_id'],)).fetchone()
        assert saved['day'] == '2026-10-05'


def test_origin_trends_use_real_related_ui_category_without_invented_feed(tmp_path, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from blogbot.creator_trends import consult, import_snapshot, validate_snapshot
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 13, 9, tzinfo=ZoneInfo('Asia/Seoul')).astimezone(tz)
    monkeypatch.setattr('blogbot.creator_trends.datetime', Clock)
    monkeypatch.delenv('BLOG_CREATOR_TRENDS_JSON', raising=False)
    s = settings(tmp_path, monkeypatch)
    monkeypatch.setattr('blogbot.creator_trends.today_kst', lambda: date(2026, 10, 13))
    snapshot = {'version': 'creator-advisor-v1', 'channel_id': 'choijku',
                'capture_method': 'authenticated_ui', 'captured_at': '2026-10-13T08:30:00+09:00',
                'groups': [{'category': 'origins', 'ui_category': '맛집',
                            'metric': 'search_inflow_rank', 'data_date': '2026-10-12',
                            'source_url': 'https://creator-advisor.naver.com/naver_blog/choijku/trends',
                            'keyword': '실비김치', 'entries': [
                                {'rank': 1, 'title': '실비김치 이름과 음식 이야기',
                                 'url': 'https://blog.naver.com/publicauthor/12345', 'views': None}]}]}
    file = tmp_path/'trends.json'
    file.write_text(json.dumps(snapshot))
    import_snapshot(s, file)
    req = request(benchmark_query='실비김치')
    with connect_db(s.db_path) as conn:
        output = consult(s, conn, [req], 1)
    assert len(output) == 1 and output[0].id == req.id
    assert output[0].provenance['creator_trends']['matches'][0]['ui_category'] == '맛집'
    assert output[0].provenance['creator_trends']['matches'][0]['views'] is None
    snapshot['groups'][0]['ui_category'] = '이름의 유래'
    with pytest.raises(ValueError, match='Wrong UI category'):
        validate_snapshot(json.dumps(snapshot))
