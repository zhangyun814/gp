from __future__ import annotations

from collections import deque
from typing import Any


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _rolling_extreme(values: list[float], period: int, maximum: bool) -> list[float]:
    result: list[float] = []
    indexes: deque[int] = deque()
    for index, value in enumerate(values):
        while indexes and indexes[0] <= index - period:
            indexes.popleft()
        while indexes and ((value >= values[indexes[-1]]) if maximum else
                           (value <= values[indexes[-1]])):
            indexes.pop()
        indexes.append(index)
        result.append(values[indexes[0]])
    return result


def _rolling_sum(values: list[float], period: int) -> list[float]:
    result: list[float] = []
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= period:
            total -= values[index - period]
        result.append(total)
    return result


def calculate_zzr(rows: list[dict[str, Any]],
                  volume_ratio_max: float = 4.0,
                  deviation_max: float = 1.18,
                  require_same_day_breakout: bool = False,
                  ) -> list[dict[str, Any]]:
    """Calculate the original close-confirmed 紫紫红 strategy without future data.

    Tunable filters (defaults reproduce the original behaviour):
    - volume_ratio_max: upper bound of the 5-day volume ratio on breakout.
    - deviation_max: upper bound of close / EMA20 on breakout.
    - require_same_day_breakout: only accept signals on the breakout day itself
      instead of within the following 2 days.
    """
    if not rows:
        return []
    opens = [float(row["open"]) for row in rows]
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    closes = [float(row["close"]) for row in rows]
    volumes = [float(row.get("volume") or 0) for row in rows]

    price = [(3 * closes[i] + highs[i] + lows[i] + opens[i]) / 6 for i in range(len(rows))]
    e35 = _ema(price, 35)
    l1 = [sum(items) / 3 for items in zip(
        _rolling_extreme(e35, 10, False),
        _rolling_extreme(e35, 30, False),
        _rolling_extreme(e35, 90, False),
    )]
    l2 = [sum(items) / 3 for items in zip(
        _rolling_extreme(l1, 5, False),
        _rolling_extreme(l1, 15, False),
        _rolling_extreme(l1, 30, False),
    )]
    t0 = _ema(_ema(price, 2), 2)

    fast, slow = _ema(closes, 6), _ema(closes, 18)
    mom = [500 * (fast[i] - slow[i]) / slow[i] if slow[i] else 0 for i in range(len(rows))]
    mom_signal = _ema(mom, 3)
    e5, e10, e20, e60 = (_ema(closes, n) for n in (5, 10, 20, 60))

    volume_sum5 = _rolling_sum(volumes, 5)
    volume_ma5 = [volume_sum5[i] / min(i + 1, 5) for i in range(len(rows))]
    high20 = _rolling_extreme(highs, 20, True)
    mfv = [volumes[i] * (2 * closes[i] - highs[i] - lows[i]) /
           max(highs[i] - lows[i], 0.01) for i in range(len(rows))]
    mfv20, volume20 = _rolling_sum(mfv, 20), _rolling_sum(volumes, 20)
    cmf = [mfv20[i] / volume20[i] if volume20[i] else 0 for i in range(len(rows))]

    trend: list[bool] = []
    breakout_raw: list[bool] = []
    momentum_red: list[bool] = []
    volume_ratio: list[float] = []
    for i in range(len(rows)):
        ratio = volumes[i] / volume_ma5[i] if volume_ma5[i] else 0
        volume_ratio.append(ratio)
        trend.append(i > 0 and t0[i] > l2[i] and mom[i] > 0 and
                     mom[i] > mom_signal[i] and mom_signal[i] > mom_signal[i - 1])
        previous_high = high20[i - 1] if i else highs[i]
        breakout_raw.append(i > 0 and closes[i] > previous_high and closes[i] > opens[i] and
                            ratio > 1.5 and ratio < volume_ratio_max and
                            e5[i] > e10[i] > e20[i] > e60[i] and
                            closes[i] / e20[i] < deviation_max)
        momentum_red.append(i > 0 and cmf[i] > 0.05 and cmf[i] > cmf[i - 1] and
                            closes[i] > e20[i] and e20[i] > e20[i - 1])

    if require_same_day_breakout:
        breakout = list(breakout_raw)
    else:
        breakout = [any(breakout_raw[max(0, i - 2):i + 1]) for i in range(len(rows))]
    active = [(i + 1) > 130 and trend[i] and breakout[i] and momentum_red[i]
              for i in range(len(rows))]
    signal = [value and (i == 0 or not active[i - 1]) for i, value in enumerate(active)]
    return [{
        "trend_purple": trend[i],
        "breakout_purple": breakout[i],
        "momentum_red": momentum_red[i],
        "active": active[i],
        "signal": signal[i],
        "mom": round(mom[i], 6),
        "mom_signal": round(mom_signal[i], 6),
        "cmf": round(cmf[i], 6),
        "volume_ratio": round(volume_ratio[i], 4),
        "breakout_level": round(high20[i - 1], 6) if i else None,
    } for i in range(len(rows))]
