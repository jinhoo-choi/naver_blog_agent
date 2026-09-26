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
