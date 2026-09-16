from __future__ import annotations

from datetime import date
from typing import Any


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def fetch_akshare_quotes(stock_code: str, start_date: date, end_date: date, adjust_type: str = "qfq") -> list[dict]:
    """Fetch daily A-share rows from optional AKShare dependency."""
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("未安装 AKShare；CSV 导入可直接使用，AKShare 请安装 requirements-market.txt") from exc
    adjust = "" if adjust_type == "none" else adjust_type
    frame = ak.stock_zh_a_hist(symbol=stock_code, period="daily",
                               start_date=start_date.strftime("%Y%m%d"),
                               end_date=end_date.strftime("%Y%m%d"), adjust=adjust)
    rows = []
    for row in frame.to_dict("records"):
        rows.append({
            "code": stock_code,
            "date": str(row.get("日期")),
            "open": _number(row.get("开盘")), "high": _number(row.get("最高")),
            "low": _number(row.get("最低")), "close": _number(row.get("收盘")),
            "volume": _number(row.get("成交量")), "amount": _number(row.get("成交额")),
            "turnover_rate": _number(row.get("换手率")), "adjust_type": adjust_type,
        })
    return [row for row in rows if row["date"] and row["date"] != "None" and row["close"] > 0]

