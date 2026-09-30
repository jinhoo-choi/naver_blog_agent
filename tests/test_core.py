from blogbot.core import choose_categories, jaccard_similarity


def test_similarity_detects_near_duplicate():
    a = "3개월 아기 수면시간 밤잠 늘리는 방법"
    b = "3개월 아기 밤잠과 수면시간 늘리는 방법"
    assert jaccard_similarity(a, b) > 0.5


def test_category_limits():
    config = {
        "categories": {
            "parenting": {"weight": 4, "max_daily": 2},
            "investment": {"weight": 3, "max_daily": 1},
            "cooking": {"weight": 3, "max_daily": 2},
        }
    }
    result = choose_categories(config, 5)
    assert len(result) == 5
    assert result.count("parenting") <= 2
    assert result.count("investment") <= 1
    assert result.count("cooking") <= 2


def test_structure_and_html_renderer_reject_model_image_urls():
    from blogbot.core import PostDraft, today_kst
    from blogbot.presentation import markdown_html, validate_structure

    post = PostDraft(
        "parenting", "육아생활", "호명", "호명반응 기준",
        "이름을 부를 때의 반응은 월령과 상황을 함께 살펴봐요.\n\n"
        "## 핵심 답변\n\n### 기준\n\n설명입니다. " + "근거 있는 문장입니다. " * 160
        + "\n\n## 관찰 방법\n\n설명\n\n## 함께 놀기\n\n설명\n\n## 상담 기준\n\n설명",
        [], ["https://www.cdc.gov/"], today_kst().isoformat(),
    )
    validate_structure(post)
    rendered = markdown_html("## <안전>\n\n본문 <script>alert(1)</script>")
    assert "<script>" not in rendered and "&lt;script&gt;" in rendered
    post.body += "\n\n![외부](https://example.com/a.jpg)"
    import pytest
    with pytest.raises(ValueError, match="Images are placed"):
        validate_structure(post)
