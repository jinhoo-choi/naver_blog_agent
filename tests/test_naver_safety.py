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
