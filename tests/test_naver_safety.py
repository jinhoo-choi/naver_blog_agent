from pathlib import Path


def test_no_publish_automation_exists():
    source = (Path(__file__).parents[1] / "src/blogbot/naver.py").read_text(encoding="utf-8")
    forbidden_call_patterns = [
        'get_by_role("button", name="발행"',
        "get_by_role('button', name='발행'",
        'get_by_text("발행", exact=True)',
        "get_by_text('발행', exact=True)",
        "click_publish(",
    ]
    for pattern in forbidden_call_patterns:
        assert pattern not in source


def test_category_routes_are_required():
    from blogbot.naver import NaverDraftWriter

    writer = NaverDraftWriter("owner", "profile", categories={
        "parenting": {"naver_category_no": 1},
        "cooking": {"naver_category_no": 6},
        "investment": {"naver_category_no": 7},
    })
    assert writer.editor_url("parenting").endswith("categoryNo=1")
    assert writer.editor_url("investment").endswith("categoryNo=7")


def test_clipboard_fallback_preserves_paragraphs_entities_inline_and_tables():
    from blogbot.naver import NaverDraftWriter, clipboard_plain_text

    source = '<p>한 <b>단어</b> &amp; 조건</p><p><br></p><hr><p>다음<br>줄입니다.</p>'
    assert clipboard_plain_text(source) == '한 단어 & 조건\n\n다음\n줄입니다.'
    assert clipboard_plain_text('<table><tr><th>항목</th><th>조건</th></tr>'
                                '<tr><td>A</td><td>B</td></tr></table>') == '항목\t조건\nA\tB'
    assert clipboard_plain_text('<p>&lt;script&gt; &nbsp; "인용"</p>') == '<script>   "인용"'
    calls = []
    class Editor:
        def evaluate(self, script, payload):
            calls.append((script, payload))
    NaverDraftWriter._write_clipboard(Editor(), source)
    assert calls[0][1] == {'html': source, 'plain': clipboard_plain_text(source)}
    assert 'value.plain' in calls[0][0]
