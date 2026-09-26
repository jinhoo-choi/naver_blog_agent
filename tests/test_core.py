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
