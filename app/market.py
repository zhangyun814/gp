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

    def rows_from_frame(frame, columns: dict[str, str]) -> list[dict]:
        rows = []
        for item in frame.to_dict("records"):
            row = {
                "code": stock_code,
                "date": str(item.get(columns["date"])),
                "open": _number(item.get(columns["open"])), "high": _number(item.get(columns["high"])),
                "low": _number(item.get(columns["low"])), "close": _number(item.get(columns["close"])),
                "volume": _number(item.get(columns["volume"])), "amount": _number(item.get(columns["amount"])),
                "turnover_rate": _number(item.get(columns["turnover"])), "adjust_type": adjust_type,
            }
            if row["date"] and row["date"] != "None" and row["close"] > 0:
                rows.append(row)
        return rows

    try:
        frame = ak.stock_zh_a_hist(symbol=stock_code, period="daily",
                                   start_date=start_date.strftime("%Y%m%d"),
                                   end_date=end_date.strftime("%Y%m%d"), adjust=adjust)
        rows = rows_from_frame(frame, {"date": "日期", "open": "开盘", "high": "最高", "low": "最低",
                                       "close": "收盘", "volume": "成交量", "amount": "成交额", "turnover": "换手率"})
        if rows:
            return rows
    except Exception:
        pass

    # ponytail: keep one small fallback for proxies that break Eastmoney's K-line endpoint.
    symbol = ("sh" if stock_code.startswith("6") else "sz") + stock_code
    try:
        frame = ak.stock_zh_a_daily(symbol=symbol, start_date=start_date.strftime("%Y%m%d"),
                                    end_date=end_date.strftime("%Y%m%d"), adjust=adjust)
    except Exception as exc:
        # A failed fallback is retryable; an empty successful frame means no
        # rows for this code/window and is handled as normal no-data below.
        raise RuntimeError(f"AKShare history request failed for {stock_code}") from exc
    return rows_from_frame(frame, {"date": "date", "open": "open", "high": "high", "low": "low",
                                   "close": "close", "volume": "volume", "amount": "amount", "turnover": "turnover"})


def fetch_akshare_stock_master() -> list[dict[str, str]]:
    """Fetch the A-share code/name master used for topic name recognition."""
    try:
        import akshare as ak
    except ImportError as exc:
        raise RuntimeError("未安装 AKShare，无法同步股票名称表") from exc
    rows = []
    for item in ak.stock_info_a_code_name().to_dict("records"):
        code, name = str(item.get("code") or "").strip(), str(item.get("name") or "").strip()
        if code.isdigit() and name:
            rows.append({"code": code.zfill(6), "name": name})
    return rows
