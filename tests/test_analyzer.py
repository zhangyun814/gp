from datetime import date
from decimal import Decimal
from app.analyzer import CODE_RE, keyword_matches


def test_stock_code_extraction():
    assert CODE_RE.findall("关注 600519 和 000001，排除 1234567") == ["600519", "000001"]


def test_rise_threshold_math():
    base = Decimal("10")
    close = Decimal("11")
    assert close / base - 1 == Decimal("0.1")


def test_keyword_normalization_does_not_duplicate_topic_links():
    assert keyword_matches("产品涨价", set()) == {"涨价": "涨价"}


def test_keyword_matching_accepts_editable_dictionary():
    assert keyword_matches("重点推荐深信服", set(), {"重点推荐": "重点推荐"}) == {"重点推荐": "重点推荐"}
