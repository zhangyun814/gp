"""Backtest the close-confirmed 紫紫红 strategy on data already in the database.

Assumptions (deliberately conservative):
- Signal fires at close of day T (calculate_zzr is close-confirmed).
- Entry: next trading day (T+1) open price. If T+1 has no bar (suspension at
  the start), the trade is skipped. If T+1 is the latest bar overall, skipped.
- T+1 rule: the earliest possible sell is T+2 (A-share T+1 settlement).
- Exit (first condition met wins, evaluated from T+2 onward):
    * stop loss: day low <= stop price  -> sell at min(open, stop price)
    * trailing/mean exit: close < EMA-N (default off unless --exit-ema given)
    * time exit: held --hold-days bars  -> sell at that day's open

Usage (inside the app container):
    python scripts/backtest_zzr.py --start 2024-01-01 --end 2026-09-19
    python scripts/backtest_zzr.py --market gem --exit-ema 10 --hold-days 15
"""
from __future__ import annotations

import argparse
import statistics
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Stock, StockDailyQuote
from app.zzr import calculate_zzr


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest 紫紫红 signals")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1),
                        help="entry window start (signal date), default 2024-01-01")
    parser.add_argument("--end", type=date.fromisoformat, default=None,
                        help="entry window end (signal date), default latest bar")
    parser.add_argument("--market", default="all",
                        choices=["all", "main", "gem", "star", "bse"])
    parser.add_argument("--hold-days", type=int, default=10,
                        help="time exit: sell after this many held bars")
    parser.add_argument("--stop", type=float, default=-0.07,
                        help="stop loss as fraction of entry, e.g. -0.07; 0 disables")
    parser.add_argument("--exit-ema", type=int, default=None,
                        help="sell when close falls below this EMA (e.g. 10); off by default")
    parser.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", "none"])
    parser.add_argument("--volume-ratio-max", type=float, default=4.0,
                        help="zzr filter: breakout volume ratio upper bound")
    parser.add_argument("--deviation-max", type=float, default=1.18,
                        help="zzr filter: close/EMA20 upper bound on breakout")
    parser.add_argument("--same-day-breakout", action="store_true",
                        help="only signal on the breakout day itself")
    return parser.parse_args()


def market_prefixes(market: str) -> tuple[str, ...] | None:
    return {
        "main": ("600", "601", "603", "605", "000", "001", "002", "003"),
        "gem": ("300", "301"),
        "star": ("688",),
        "bse": ("4", "8", "920"),
    }.get(market)


def ema(values: list[float], period: int) -> list[float]:
    if period <= 0 or not values:
        return [0.0] * len(values)
    alpha = 2 / (period + 1)
    out = [values[0]]
    for value in values[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def main() -> int:
    args = parse_args()
    session = SessionLocal()
    try:
        statement = select(Stock).order_by(Stock.stock_code)
        prefixes = market_prefixes(args.market)
        stocks = [s for s in session.scalars(statement)
                  if prefixes is None or s.stock_code.startswith(prefixes)]
        stock_by_id = {s.id: s for s in stocks}
        print(f"stocks to scan: {len(stocks)} (market={args.market}, adjust={args.adjust})")

        quote_statement = (select(StockDailyQuote)
                           .where(StockDailyQuote.stock_id.in_(list(stock_by_id)),
                                  StockDailyQuote.adjust_type == args.adjust)
                           .order_by(StockDailyQuote.stock_id, StockDailyQuote.trade_date))

        trades: list[dict] = []
        signals_seen = 0
        current_id: int | None = None
        rows: list[StockDailyQuote] = []

        def process(stock_id: int | None, bars: list[StockDailyQuote]) -> None:
            nonlocal signals_seen
            if stock_id is None or len(bars) < 131:
                return
            points = calculate_zzr([{
                "open": float(b.open), "high": float(b.high), "low": float(b.low),
                "close": float(b.close), "volume": float(b.volume or 0),
            } for b in bars],
                volume_ratio_max=args.volume_ratio_max,
                deviation_max=args.deviation_max,
                require_same_day_breakout=args.same_day_breakout)
            closes = [float(b.close) for b in bars]
            exit_ema = ema(closes, args.exit_ema) if args.exit_ema else None
            for index, point in enumerate(points):
                if not point["signal"]:
                    continue
                signal_date = bars[index].trade_date
                if index + 1 >= len(bars):
                    continue  # no next-day bar yet: cannot enter
                entry_bar = bars[index + 1]
                entry = float(entry_bar.open)
                if entry <= 0:
                    continue
                if args.end and signal_date > args.end:
                    continue
                if signal_date < args.start:
                    continue
                signals_seen += 1
                stop_price = entry * (1 + args.stop) if args.stop else None
                sell_index = None
                sell_price = None
                exit_reason = None
                last = min(index + 1 + args.hold_days, len(bars) - 1)
                for held in range(index + 2, last + 1):
                    bar = bars[held]
                    open_, high, low, close = (float(bar.open), float(bar.high),
                                               float(bar.low), float(bar.close))
                    if stop_price is not None and low <= stop_price:
                        sell_index, sell_price = held, min(open_, stop_price)
                        exit_reason = "stop"
                        break
                    if exit_ema is not None and close < exit_ema[held]:
                        sell_index, sell_price, exit_reason = held, close, "ema_exit"
                        break
                    if held == index + 1 + args.hold_days or held == len(bars) - 1:
                        sell_index, sell_price, exit_reason = held, open_, "time" \
                            if held == index + 1 + args.hold_days else "data_end"
                        break
                if sell_index is None:  # data ended before time exit
                    continue
                trades.append({
                    "code": stock_by_id[stock_id].stock_code,
                    "name": stock_by_id[stock_id].stock_name,
                    "signal_date": signal_date, "entry_date": entry_bar.trade_date,
                    "exit_date": bars[sell_index].trade_date,
                    "entry": entry, "exit": sell_price,
                    "return": sell_price / entry - 1, "reason": exit_reason,
                    "held": sell_index - index - 1,
                })

        for row in session.scalars(quote_statement).yield_per(4000):
            if current_id is not None and row.stock_id != current_id:
                process(current_id, rows)
                rows = []
            current_id = row.stock_id
            rows.append(row)
        process(current_id, rows)
    finally:
        session.close()

    if not trades:
        print(f"no completed trades (signals seen: {signals_seen})")
        return 0

    returns = [t["return"] for t in trades]
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    print(f"\n=== 紫紫红回测 {args.start} ~ {args.end or '最新'} ===")
    print(f"信号数（可入场）: {signals_seen}   完成交易: {len(trades)}")
    print(f"胜率: {len(wins) / len(trades) * 100:.1f}%")
    print(f"平均收益: {statistics.mean(returns) * 100:.2f}%   "
          f"中位: {statistics.median(returns) * 100:.2f}%")
    print(f"单笔最大盈利: {max(returns) * 100:.1f}%   最大亏损: {min(returns) * 100:.1f}%")
    if gross_loss:
        print(f"盈亏比 (profit factor): {gross_win / gross_loss:.2f}")
    print(f"平均持有: {statistics.mean(t['held'] for t in trades):.1f} 天")

    by_reason: dict[str, list[float]] = defaultdict(list)
    for trade in trades:
        by_reason[trade["reason"]].append(trade["return"])
    print("\n退出方式分布:")
    for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        print(f"  {reason}: {len(items)} 笔, 平均 {statistics.mean(items) * 100:.2f}%")

    by_year: dict[int, list[float]] = defaultdict(list)
    for trade in trades:
        by_year[trade["signal_date"].year].append(trade["return"])
    print("\n按信号年份:")
    for year in sorted(by_year):
        items = by_year[year]
        print(f"  {year}: {len(items)} 笔, 胜率 "
              f"{sum(1 for r in items if r > 0) / len(items) * 100:.0f}%, "
              f"平均 {statistics.mean(items) * 100:.2f}%")

    trades.sort(key=lambda t: t["return"])
    print("\n最差 5 笔:")
    for trade in trades[:5]:
        print(f"  {trade['code']} {trade['name']} {trade['signal_date']} "
              f"{trade['return'] * 100:.1f}% ({trade['reason']})")
    print("最好 5 笔:")
    for trade in trades[-5:][::-1]:
        print(f"  {trade['code']} {trade['name']} {trade['signal_date']} "
              f"{trade['return'] * 100:.1f}% ({trade['reason']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
