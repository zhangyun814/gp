from app.zzr import calculate_zzr


def _rows(count: int = 170) -> list[dict]:
    rows = []
    for index in range(count):
        close = 10 + index * 0.02
        rows.append({"open": close - 0.03, "high": close + 0.08,
                     "low": close - 0.22, "close": close, "volume": 1000})
    rows[150].update({"open": 12.8, "high": 14.9, "low": 12.7,
                      "close": 14.75, "volume": 2300})
    return rows


def test_zzr_emits_close_confirmed_signal_without_changing_history():
    rows = _rows()
    points = calculate_zzr(rows)
    signal_indexes = [index for index, point in enumerate(points) if point["signal"]]
    assert signal_indexes
    index = signal_indexes[0]
    assert index >= 130
    assert points[index]["trend_purple"]
    assert points[index]["breakout_purple"]
    assert points[index]["momentum_red"]

    extended = calculate_zzr(rows + [{"open": 20, "high": 21, "low": 19,
                                      "close": 20.5, "volume": 2000}])
    assert extended[:len(points)] == points
