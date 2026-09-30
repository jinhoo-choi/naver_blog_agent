import json
from types import SimpleNamespace

import pytest

from blogbot import cloud
from blogbot.core import PostDraft
from blogbot.presentation import (
    markdown_html,
    normalize_structure,
    render_segments,
    validate_structure,
)


@pytest.mark.parametrize('status,failed', [('ERROR',True),('IMAGES_PENDING',True),
    ('COMMUNITY_SOURCE_PENDING',True),
    ('RESEARCH_REQUIRED',True),('APPROVED',False),('DROP_REVIEW',True),
    ('NO_ELIGIBLE_INPUT_OR_DAILY_LIMIT',False)])
def test_cloud_nonzero_for_failed_preparation_and_always_packs(tmp_path, monkeypatch, status, failed):
    settings = SimpleNamespace(db_path=tmp_path/'blog.db')
    monkeypatch.setattr('sys.argv', ['cloud','prepare'])
    monkeypatch.setattr(cloud,'load_settings',lambda:settings)
    monkeypatch.setattr(cloud,'restore',lambda _:None)
    monkeypatch.setattr(cloud,'seed_inputs',lambda _:None)
    monkeypatch.setattr(cloud,'run_daily',lambda *a,**k:[{'status':status}])
    packs=[]
    monkeypatch.setattr(cloud,'pack',lambda *a:packs.append(a))
    if failed:
        with pytest.raises(RuntimeError): cloud.main()
    else:
        cloud.main()
    assert len(packs)==1
    assert json.loads((tmp_path/'run-summary.json').read_text())['failed'] is failed


def test_separators_at_major_sections_and_references_only():
    body='Opening\n\n## First\nFirst paragraph\n\nNext paragraph\n\n### Detail\nText\n\n## Second\nText'
    html=markdown_html(body)
    assert html.count('<hr>')==2
    assert html.index('<hr>') < html.index('First')
    post=PostDraft('parenting','육아생활','test','title',body,[],[],'2026-09-28')
    segments=render_segments(post)
    assert sum(s.html.count('<hr>') for s in segments)==3
    assert segments[-1].html.startswith('<hr>')
    assert '&lt;script&gt;' in markdown_html('<script>alert(1)</script>')


def test_structure_repair_preserves_text_and_existing_titles():
    body = 'Opening\n\n' + '\n\n'.join(f'## Existing {i}\nVerified fact {i}' for i in range(5))
    post = PostDraft('investment', '시장·산업', 'topic', 'title', body, [], [], '2026-09-30')
    fixed = normalize_structure(post)
    validate_structure(fixed)
    assert fixed.body == body.replace('## Existing 4', '### Existing 4')
    assert normalize_structure(fixed) == fixed


def test_investment_image_prompt_excludes_parenting_scene_instructions():
    from dataclasses import replace

    from blogbot.images import image_prompt
    post = PostDraft('investment', '시장·산업', 'topic', 'AI칩 담보대출 보험', 'body', [], [], '2026-09-30')
    prompt = image_prompt(post, '리스크')
    assert 'Objects only' in prompt and 'microchip protected by' in prompt
    assert 'onesie' not in prompt and 'round heads' not in prompt
    assert 'onesie' in image_prompt(replace(post, category='parenting', title='아기 손가락'), '수면')


def test_restore_uses_creation_time_instead_of_artifact_id(tmp_path, monkeypatch):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z: z.writestr('bundle.enc', b'placeholder')
    artifacts = [{'id': 100, 'name': 'blog-state-old', 'expired': False,
                  'created_at': '2026-09-30T01:00:00Z'},
                 {'id': 90, 'name': 'blog-state-new', 'expired': False,
                  'created_at': '2026-09-30T02:00:00Z'}]
    requests = []
    def get(path):
        requests.append(path)
        return json.dumps({'artifacts': artifacts}).encode() if 'per_page' in path else buffer.getvalue()
    monkeypatch.setattr(cloud, 'github_get', get)
    monkeypatch.setattr(cloud, 'extract_bundle', lambda *a: None)
    monkeypatch.setenv('BLOG_BUNDLE_KEY', 'placeholder')
    cloud.restore(tmp_path)
    assert requests[-1] == '/actions/artifacts/90/zip'


def test_missing_category_is_failure_even_without_an_api_exception(tmp_path, monkeypatch):
    settings = SimpleNamespace(db_path=tmp_path/'blog.db', config={'categories': {
        'parenting': {}, 'exercise': {}, 'investment': {}, 'cooking': {}}})
    monkeypatch.setattr('sys.argv', ['cloud', 'prepare'])
    monkeypatch.setattr(cloud, 'load_settings', lambda: settings)
    monkeypatch.setattr(cloud, 'restore', lambda _: None)
    monkeypatch.setattr(cloud, 'seed_inputs', lambda _: None)
    monkeypatch.setattr(cloud, 'run_daily', lambda *a, **k: [{'status': 'NO_ELIGIBLE_INVESTMENT'}])
    monkeypatch.setattr(cloud, 'pack', lambda *a: None)
    with pytest.raises(RuntimeError):
        cloud.main()
    summary = json.loads((tmp_path/'run-summary.json').read_text())
    assert summary['failed']
    assert summary['results'][-1] == {'status': 'PREPARATION_PARTIAL',
                                    'missing_categories': ['exercise', 'investment', 'parenting']}


def test_preparation_failure_retains_detailed_alert_without_generic_duplicate(tmp_path, monkeypatch):
    settings = SimpleNamespace(db_path=tmp_path/'blog.db')
    monkeypatch.setattr('sys.argv', ['cloud', 'prepare'])
    monkeypatch.setattr(cloud, 'load_settings', lambda: settings)
    monkeypatch.setattr(cloud, 'restore', lambda _: None)
    monkeypatch.setattr(cloud, 'seed_inputs', lambda _: None)
    monkeypatch.setattr(cloud, 'pack', lambda *a: None)
    failure = {'status': 'ERROR', 'stage': 'writer', 'reason': 'invalid_reference_date'}
    monkeypatch.setattr(cloud, 'run_daily', lambda *a, **k: [failure])
    alerts = []
    monkeypatch.setattr('blogbot.notify.telegram', lambda results: alerts.append(results))
    with pytest.raises(cloud.PreparationFailed):
        cloud.main()
    assert alerts == [[failure]]


def test_notification_distinguishes_preparation_from_save_and_shows_reason(tmp_path, monkeypatch):
    from blogbot.notify import telegram
    path = tmp_path/'summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(path))
    monkeypatch.setenv('BLOG_NOTIFY_ENABLED', 'false')
    telegram([{'status': 'APPROVED'}, {'status': 'ERROR', 'category': 'exercise',
                                     'stage': 'writer', 'error': 'APITimeoutError'}])
    text = path.read_text()
    assert '원고 준비 1건 / 네이버 임시저장 0건' in text
    assert 'exercise / writer / ERROR / APITimeoutError' in text


def test_probe_never_runs_daily_or_writes_naver(tmp_path, monkeypatch):
    settings = SimpleNamespace(db_path=tmp_path/'blog.db', openai_api_key='test', openai_model='gpt-5')
    monkeypatch.setattr('sys.argv', ['cloud','probe'])
    monkeypatch.setattr(cloud,'load_settings',lambda:settings)
    monkeypatch.setattr(cloud,'restore',lambda _:None)
    monkeypatch.setattr(cloud,'seed_inputs',lambda _:None)
    monkeypatch.setattr(cloud,'run_daily',lambda *a,**k:pytest.fail('No daily run in probe'))
    monkeypatch.setattr('openai.OpenAI', lambda **kwargs:object())
    calls=[]
    def request(*a,**kw):
        calls.append(kw)
        return {'records':[]}, SimpleNamespace(status='completed')
    monkeypatch.setattr('blogbot.responses.request_json',request)
    packs=[]
    monkeypatch.setattr(cloud,'pack',lambda *a:packs.append(a))
    cloud.main()
    assert len(calls)==1 and calls[0]['max_tool_calls']==1
    assert 'retry_output_tokens' not in calls[0]
    assert len(packs)==1


def test_manual_save_receipts_are_idempotent_and_consume_old_failed_input(tmp_path):
    from blogbot.core import connect_db
    (tmp_path/'config').mkdir()
    path=tmp_path/'config/manual-saves.json'
    path.write_text(json.dumps({'records':[{'request_id':'manual-1','day':'2026-09-28',
        'category':'parenting','status':'SAVED_NAVER'}]}))
    settings=SimpleNamespace(root=tmp_path,db_path=tmp_path/'blog.db',
                             config={'categories':{'parenting':{}}})
    conn=connect_db(settings.db_path)
    with conn:
        conn.execute("INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)",
                     ('2026-09-28','parenting','manual-1','ERROR'))
    cloud.import_manual_saves(settings)
    cloud.import_manual_saves(settings)
    assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0]==1
    assert conn.execute('SELECT status FROM attempts').fetchone()[0]=='SAVED_NAVER'
    conn.close()


def test_unresolved_error_cannot_be_hidden_by_noop_rerun(tmp_path, monkeypatch):
    from blogbot.core import connect_db, today_kst

    settings = SimpleNamespace(db_path=tmp_path/'blog.db')
    conn = connect_db(settings.db_path)
    with conn:
        conn.execute('INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,?)',
                     (str(today_kst()), 'investment', 'failed', 'ERROR'))
    conn.close()
    monkeypatch.setattr('sys.argv', ['cloud', 'prepare'])
    monkeypatch.setattr(cloud, 'load_settings', lambda: settings)
    monkeypatch.setattr(cloud, 'restore', lambda _: None)
    monkeypatch.setattr(cloud, 'seed_inputs', lambda _: None)
    monkeypatch.setattr(cloud, 'run_daily', lambda *a, **k: [])
    packs = []
    monkeypatch.setattr(cloud, 'pack', lambda *a: packs.append(a))
    with pytest.raises(RuntimeError):
        cloud.main()
    summary = json.loads((tmp_path/'run-summary.json').read_text())
    assert summary['failed'] and len(packs) == 1
    assert summary['results'] == [{'status': 'UNRESOLVED_FAILED_ATTEMPTS', 'count': 1}]
