from datetime import date
from decimal import Decimal
from app.analyzer import CODE_RE


def test_stock_code_extraction():
    assert CODE_RE.findall("关注 600519 和 000001，排除 1234567") == ["600519", "000001"]


def test_rise_threshold_math():
    base = Decimal("10")
    close = Decimal("11")
    assert close / base - 1 == Decimal("0.1")
