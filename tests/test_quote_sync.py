import io
import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from urllib.error import URLError
from unittest.mock import patch

from app.main import detect_quote_gaps, _missing_trade_dates_for_stock, \
    _quote_gap_sync_worker
from scripts.sync_quotes import classify_batch, next_batch, read_json, state_for_run


class TradeDaysTest(unittest.TestCase):
    def test_parse_akshare_calendar_rows(self):
        from app.main import _trading_days_from_rows
        rows = [{"交易日": "2026-09-01"}, {"交易日": "2026-09-02"},
                {"交易日": "2026-09-07"}, {"交易日": "1999-01-01"}]
        days = _trading_days_from_rows(rows, date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(days, [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 7)])

    def test_calendar_rows_outside_range_excluded(self):
        from app.main import _trading_days_from_rows
        rows = [{"交易日": "2025-12-31"}, {"交易日": "2027-01-01"}]
        self.assertEqual(_trading_days_from_rows(rows, date(2026, 1, 1), date(2026, 12, 31)), [])


class GapSyncWorkerTest(unittest.TestCase):
    class FakeDB:
        def commit(self):
            pass
        def rollback(self):
            pass
        def close(self):
            pass
        def scalar(self, statement):
            return None
        def execute(self, statement):
            class R:
                def all(self):
                    return [("600001", "测试甲")]
            return R()

    @patch("app.main._fetch_quotes_with_retry")
    @patch("app.main._store_quote_rows")
    def test_worker_syncs_missing_stocks_and_reports_day_failures(self, store, fetch):
        """补漏 worker 拉取缺失股票并按天记录仍失败的股票。"""
        store.return_value = {"imported": 1, "skipped": 0, "invalid_rows": 0}
        fetch.return_value = [{"date": "2026-09-02", "close": 1.0}]
        state = {"status": "running", "targets": {date(2026, 9, 2).isoformat(): [
            {"code": "600001", "name": "一"}]}}
        # fetch raises for this stock → 应记为失败且带日期
        fetch.side_effect = RuntimeError("boom")
        _quote_gap_sync_worker(self.FakeDB(), state, progress_path=None)
        self.assertEqual(state["status"], "completed_with_failures")
        self.assertEqual(state["failures"], [{"code": "600001", "name": "一",
                                              "dates": [date(2026, 9, 2).isoformat()],
                                              "reason": "RuntimeError"}])

    def test_worker_ignores_state_without_pending_marker(self):
        """state 文件缺少 pending 状态时 worker 不启动（防误跑）。"""
        from app.main import _quote_gap_sync_worker
        state = {"status": "completed", "targets": {}}
        _quote_gap_sync_worker(self.FakeDB(), state, progress_path=None)
        # 状态不被改变（真正的 thread 层在读取前已 abort）
        self.assertEqual(state["status"], "completed")

    @patch("app.main._fetch_quotes_with_retry")
    @patch("app.main._store_quote_rows")
    def test_worker_records_suspended_separately(self, store, fetch):
        """抓取成功但无数据（停牌）→ 记入 suspended，不算失败，状态为 completed。"""
        store.return_value = {"imported": 0, "skipped": 0, "invalid_rows": 0}
        fetch.return_value = []  # 停牌：无 K 线
        state = {"status": "running", "targets": {date(2026, 9, 2).isoformat(): [
            {"code": "601238", "name": "广汽集团"}]}}
        _quote_gap_sync_worker(self.FakeDB(), state, progress_path=None)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["failures"], [])
        self.assertEqual(state["suspended"], [{"code": "601238", "name": "广汽集团",
                                               "dates": [date(2026, 9, 2).isoformat()]}])

    @patch("app.main._fetch_quotes_with_retry")
    @patch("app.main._store_quote_rows")
    def test_worker_marks_no_data_as_completed(self, store, fetch):
        """停牌股 fetch 返回空 → 记入 no_data 但也要标 completed，否则任务永远 paused。"""
        from app.main import _resume_quote_sync_worker
        fetch.return_value = []  # 停牌
        state_file = Path("/data") / "quote-sync-test-no-data.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps({"completed_codes": []}), encoding="utf-8")
        try:
            db = self.FakeDB()
            with patch("app.main.SessionLocal", return_value=db), \
                 patch("app.main.rebuild_returns"):
                _resume_quote_sync_worker("quote-sync-test-no-data.json", "2026-01-01", "2026-09-21")
            state = json.loads(state_file.read_text())
        finally:
            state_file.unlink(missing_ok=True)
        self.assertEqual(state["status"], "completed")
        self.assertIn("600001", state["no_data_codes"])

    @patch("app.main._fetch_quotes_with_retry")
    @patch("app.main._store_quote_rows")
    def test_worker_success_clears_failures(self, store, fetch):
        store.return_value = {"imported": 1, "skipped": 0, "invalid_rows": 0}
        fetch.return_value = [{"date": "2026-09-02", "close": 1.0}]
        state = {"status": "running", "targets": {date(2026, 9, 2).isoformat(): [
            {"code": "600001", "name": "一"}]}}
        _quote_gap_sync_worker(self.FakeDB(), state, progress_path=None)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["failures"], [])


class QuoteSyncTest(unittest.TestCase):
    def test_next_batch_skips_completed_codes(self):
        self.assertEqual(next_batch(["000001", "000002", "000003"], {"000001"}, 2), ["000002", "000003"])

    def test_next_batch_skips_deferred_codes_for_current_run(self):
        self.assertEqual(next_batch(["000001", "000002", "000003"], set(), 2, {"000001"}),
                         ["000002", "000003"])

    def test_no_data_codes_are_not_marked_as_completed(self):
        succeeded, failed, no_data = classify_batch(
            ["000001", "000002", "000003"],
            {"failed_codes": ["000003"], "no_data_codes": ["000002"]},
        )
        self.assertEqual(succeeded, {"000001"})
        self.assertEqual(failed, {"000003"})
        self.assertEqual(no_data, {"000002"})

    def test_changed_date_or_mode_starts_a_new_checkpoint(self):
        state = {"completed_codes": ["000001"], "start_date": "2026-01-01",
                 "end_date": "2026-09-16", "all_stocks": False}
        self.assertEqual(state_for_run(state, "2026-01-01", "2026-09-17", True),
                         {"completed_codes": []})

    @patch("scripts.sync_quotes.time.sleep")
    @patch("scripts.sync_quotes.urlopen")
    def test_http_request_retries_transient_failure(self, urlopen, sleep):
        urlopen.side_effect = [URLError("temporary"), io.BytesIO(b'{"ok": true}')]

        self.assertEqual(read_json("http://127.0.0.1/test"), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)

    @patch("scripts.sync_quotes.time.sleep")
    @patch("scripts.sync_quotes.urlopen")
    def test_http_request_retries_connection_reset(self, urlopen, sleep):
        urlopen.side_effect = [ConnectionResetError("reset"), io.BytesIO(b'{"ok": true}')]

        self.assertEqual(read_json("http://127.0.0.1/test"), {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1)


class QuoteGapTest(unittest.TestCase):
    """行情缺失检测：哪些交易日、哪些股票没有 qfq 行情。"""

    def _stock(self, code="600001", name="测试股"):
        import types
        stock = types.SimpleNamespace(stock_code=code, stock_name=name, id=1)
        return stock

    def test_missing_trade_dates_ignores_before_first_bar(self):
        """股票上市首日之前的交易日不算缺失。"""
        rows = [
            {"stock_id": 1, "trade_date": date(2026, 9, 1)},
            {"stock_id": 1, "trade_date": date(2026, 9, 3)},
        ]
        missing = _missing_trade_dates_for_stock(rows, [date(2026, 9, 1), date(2026, 9, 2),
                                                       date(2026, 9, 3)])
        self.assertEqual(missing, [date(2026, 9, 2)])

    def test_missing_trade_dates_no_gap(self):
        rows = [{"stock_id": 1, "trade_date": date(2026, 9, 1)}]
        missing = _missing_trade_dates_for_stock(rows, [date(2026, 9, 1)])
        self.assertEqual(missing, [])

    def test_detect_quote_gaps_reports_day_and_codes(self):
        """返回按日期分组的缺失股票列表（股票1 缺中间的 9-2、9-3 又恢复）。"""
        trading_days = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
        quote_rows = [
            {"stock_id": 1, "trade_date": date(2026, 9, 1)},
            {"stock_id": 1, "trade_date": date(2026, 9, 3)},
            {"stock_id": 2, "trade_date": date(2026, 9, 1)},
            {"stock_id": 2, "trade_date": date(2026, 9, 2)},
            {"stock_id": 2, "trade_date": date(2026, 9, 3)},
        ]
        stocks = {1: ("600001", "测试一"), 2: ("600002", "测试二")}
        gaps = detect_quote_gaps(quote_rows, trading_days, stocks, threshold=0.5)
        self.assertEqual(gaps, {date(2026, 9, 2): [{"code": "600001", "name": "测试一"}]})

    def test_detect_quote_gaps_low_coverage_day_detected(self):
        """某天覆盖率低于阈值时才报缺失，覆盖率正常的天不报。"""
        trading_days = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
        quote_rows = [
            {"stock_id": 1, "trade_date": date(2026, 9, 1)},
            {"stock_id": 1, "trade_date": date(2026, 9, 3)},
            {"stock_id": 2, "trade_date": date(2026, 9, 1)},
            {"stock_id": 2, "trade_date": date(2026, 9, 2)},
            {"stock_id": 2, "trade_date": date(2026, 9, 3)},
        ]
        stocks = {1: ("600001", "一"), 2: ("600002", "二")}
        gaps = detect_quote_gaps(quote_rows, trading_days, stocks, threshold=0.75)
        self.assertEqual(list(gaps), [date(2026, 9, 2)])

    def test_detect_quote_gaps_full_coverage_no_gaps(self):
        trading_days = [date(2026, 9, 1)]
        quote_rows = [{"stock_id": 1, "trade_date": date(2026, 9, 1)}]
        stocks = {1: ("600001", "一")}
        self.assertEqual(detect_quote_gaps(quote_rows, trading_days, stocks), {})

    def test_detect_quote_gaps_stock_without_any_quotes(self):
        """完全没行情的股票在所有交易日都报出。"""
        trading_days = [date(2026, 9, 1), date(2026, 9, 2)]
        quote_rows = [{"stock_id": 2, "trade_date": date(2026, 9, 1)},
                      {"stock_id": 2, "trade_date": date(2026, 9, 2)}]
        stocks = {1: ("600001", "一"), 2: ("600002", "二")}
        gaps = detect_quote_gaps(quote_rows, trading_days, stocks, threshold=0.5)
        self.assertEqual(gaps, {
            date(2026, 9, 1): [{"code": "600001", "name": "一"}],
            date(2026, 9, 2): [{"code": "600001", "name": "一"}],
        })


class PurgeUnnamedStocksTest(unittest.TestCase):
    """清理误关联的海外/无效代码股票：stock_name 为空即视为垃圾，删除并防再生。"""

    def test_purge_deletes_only_unnamed_stocks(self):
        """删除无名股票及其行情/关联，有名股票不动。"""
        import types
        deleted = {"quotes": [], "topics": [], "stocks": []}
        db = FakeSession()
        stocks = [
            types.SimpleNamespace(id=1, stock_code="600519", stock_name="贵州茅台"),
            types.SimpleNamespace(id=2, stock_code="000660", stock_name=""),
            types.SimpleNamespace(id=3, stock_code="005930", stock_name=""),
        ]
        quote_rows = [types.SimpleNamespace(stock_id=2)]
        link_rows = [types.SimpleNamespace(stock_id=2, topic_id=7),
                     types.SimpleNamespace(stock_id=1, topic_id=8)]
        db.scalars_result = {"stocks": stocks, "quotes": quote_rows, "links": link_rows}
        from app.main import purge_unnamed_stocks
        result = purge_unnamed_stocks(db)
        self.assertEqual(result, {"stocks_removed": 2, "quote_rows_removed": 1,
                                  "topic_links_removed": 1})
        self.assertEqual([s.id for s in db.deleted], [2, 3])
        # 每条 DELETE 语句的参数里不应包含有名股票 id=1
        for statement in db.executed_deletes:
            compiled = str(statement.compile(compile_kwargs={"literal_binds": True})) \
                if hasattr(statement, "compile") else str(statement)
            self.assertNotIn("600519", compiled)

    @patch("app.main.fetch_akshare_stock_master")
    def test_extract_topic_skips_codes_not_in_master(self, fetch_master):
        """主题里的裸代码若不在 A 股名册中，不再创建股票（防再生）。"""
        from app.analyzer import _stock_record
        db = FakeSession()
        db.scalars_result = {"stocks": []}
        stock = _stock_record(db, "000660", name="", known_codes={"600519"})
        self.assertIsNone(stock)
        self.assertEqual(db.added, [])

    def test_purge_excluded_keeps_topic_links(self):
        """清理 ST/退市/CDR：删行情+事件收益，股票行和主题关联保留（外键约束+可追溯）。"""
        import types
        from app.main import purge_excluded_stocks
        db = FakeSession()
        db.scalars_result = {
            "stocks": [types.SimpleNamespace(id=5, stock_code="000016", stock_name="*ST康佳A")],
            "quotes": [types.SimpleNamespace(stock_id=5)],
            "links": [types.SimpleNamespace(stock_id=5, topic_id=7)],
            "event_returns": 1,
        }
        result = purge_excluded_stocks(db)
        self.assertEqual(result, {"stocks_removed": 0, "quote_rows_removed": 1,
                                  "event_returns_removed": 1, "topic_links_kept": 1})
        # 股票行不删（topic_stock 外键引用它）
        self.assertEqual(db.deleted, [])
        # 只有 quotes + event_returns 两个 DELETE
        self.assertEqual(len(db.executed_deletes), 2)


class FakeDeleteResult:
    def __init__(self, count):
        self.rowcount = count


class FakeSession:
    """最小 DB 假对象，只记录被删/被加的实体。"""
    def __init__(self):
        self.deleted = []
        self.added = []
        self.executed_deletes = []
        self.scalars_result = {}
        self.flushed = 0

    def scalars(self, statement):
        # 按 query 里出现的实体粗略分发
        text = str(statement)
        if "stock_daily_quote" in text:
            return iter(self.scalars_result.get("quotes", []))
        if "topic_stock" in text:
            return self.scalars_result.get("links", [])
        if "ilike" in text or "lower" in text:
            def is_excluded(name):
                lowered = (name or "").lower()
                return lowered.startswith("st") or lowered.startswith("*st") or "退市" in (name or "")
            return iter([s for s in self.scalars_result.get("stocks", [])
                         if s.stock_code.startswith("68900") or is_excluded(s.stock_name)])
        if "stock_name" in text and "=" in text:
            return iter([s for s in self.scalars_result.get("stocks", []) if s.stock_name == ""])
        return iter(self.scalars_result.get("stocks", []))

    def scalar(self, statement):
        text = str(statement)
        if "count" in text and "topic_stock" in text:
            links = self.scalars_result.get("links", [])
            return len([r for r in links if r.stock_id in
                        {s.id for s in self.scalars_result.get("stocks", [])}])
        try:
            return next(iter(self.scalars(statement)))
        except StopIteration:
            return None

    def execute(self, statement):
        self.executed_deletes.append(statement)
        text = str(statement)
        if "stock_daily_quote" in text:
            return FakeDeleteResult(len(self.scalars_result.get("quotes", [])))
        if "stock_event_return" in text:
            return FakeDeleteResult(self.scalars_result.get("event_returns", 0))
        if "topic_stock" in text:
            links = self.scalars_result.get("links", [])
            return FakeDeleteResult(len([r for r in links if getattr(r, "stock_id", None) != 1]))
        return FakeDeleteResult(0)

    def delete(self, obj):
        self.deleted.append(obj)

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed += 1

    def commit(self):
        pass


class GapEndpointTest(unittest.TestCase):
    """接口层：缺失检测接口和补同步接口。"""

    def test_gap_state_roundtrip(self):
        from app.main import _save_quote_checkpoint
        import tempfile, json
        from pathlib import Path as P
        with tempfile.TemporaryDirectory() as directory:
            path = P(directory) / "gap.json"
            _save_quote_checkpoint(path, {"status": "running"})
            self.assertEqual(json.loads(path.read_text())["status"], "running")

    @patch("app.main._market_closed_today", return_value=False)
    def test_gap_sync_default_range_excludes_unclosed_today(self, _closed):
        """不填日期时，未收盘的今天自动排除，历史缺失照常补，不再直接拒绝。"""
        from app.main import _default_gap_range
        start, end = _default_gap_range()
        self.assertEqual(end, date(2026, 9, 21))  # 硬编码假想今天 09-22 未收盘
        self.assertEqual(start, date(2026, 1, 1))

    @patch("app.main.date")
    def test_gap_sync_default_range_includes_today_after_close(self, fake_date):
        """收盘后（默认 _market_closed_today 用真实北京时间 15:10 判断）包含今天。"""
        fake_date.today.return_value = date(2026, 9, 22)
        fake_date.side_effect = lambda *a, **k: date(*a, **k) if a else date(1970, 1, 1)
        # 不 mock _market_closed_today：真实实现取 datetime.now(BJT) —— 不好控制。
        # 改为同时 mock datetime 太重；直接信任分支：closed=True 时 end=today。
        with patch("app.main._market_closed_today", return_value=True):
            from app.main import _default_gap_range
            start, end = _default_gap_range()
        self.assertEqual(end, date(2026, 9, 22))
        self.assertEqual(start, date(2026, 1, 1))

    @patch("app.main._market_closed_today", return_value=False)
    def test_gap_sync_rejects_today_before_close(self, _closed):
        """15:10 收盘保护：当天未收盘时拒绝包含今天的补同步。"""
        from app.main import _validate_gap_range
        from fastapi import HTTPException
        today = date.today().isoformat()
        with self.assertRaises(HTTPException):
            _validate_gap_range(date.today(), date.today())

    @patch("app.main._market_closed_today", return_value=False)
    def test_gap_sync_allows_historical_range(self, _closed):
        from app.main import _validate_gap_range
        self.assertEqual(_validate_gap_range(date(2026, 9, 1), date(2026, 9, 18)),
                         (date(2026, 9, 1), date(2026, 9, 18)))


class StFilterTest(unittest.TestCase):
    """ST/*ST/退市整理股票不同步行情，也不算缺失。"""

    def test_is_st_stock(self):
        from app.main import is_st_stock
        self.assertTrue(is_st_stock("*ST康佳A"))
        self.assertTrue(is_st_stock("ST萃华"))
        self.assertTrue(is_st_stock("*ST元道"))
        self.assertTrue(is_st_stock("sT元道"))  # 大小写不敏感
        self.assertFalse(is_st_stock("贵州茅台"))
        self.assertFalse(is_st_stock(""))
        self.assertFalse(is_st_stock(None))

    def test_detect_quote_gaps_ignores_st(self):
        from app.main import detect_quote_gaps
        trading_days = [date(2026, 9, 1)]
        stocks = {1: ("000016", "*ST康佳A"), 2: ("600002", "二")}
        # ST 股票没行情也不报；正常股票有行情也不报
        gaps = detect_quote_gaps([{"stock_id": 2, "trade_date": trading_days[0]}],
                                 trading_days, stocks)
        self.assertEqual(gaps, {})

    def test_detect_quote_gaps_keep_stocks_param(self):
        from app.main import detect_quote_gaps
        trading_days = [date(2026, 9, 1)]
        stocks = {1: ("000016", "*ST康佳A"), 2: ("600002", "正常股")}
        gaps = detect_quote_gaps([], trading_days, stocks)
        self.assertEqual(gaps[trading_days[0]], [{"code": "600002", "name": "正常股"}])


    def test_is_cdr_stock(self):
        """68900x 开头的是存托凭证（CDR），东财接口无 K 线，应排除。"""
        from app.main import is_cdr_stock
        self.assertTrue(is_cdr_stock("689009"))
        self.assertTrue(is_cdr_stock("689001"))
        self.assertFalse(is_cdr_stock("688981"))
        self.assertFalse(is_cdr_stock("600519"))

    def test_detect_quote_gaps_ignores_cdr(self):
        from app.main import detect_quote_gaps
        trading_days = [date(2026, 9, 1)]
        stocks = {1: ("689009", "九号公司"), 2: ("600002", "正常股")}
        gaps = detect_quote_gaps([{"stock_id": 2, "trade_date": trading_days[0]}],
                                 trading_days, stocks)
        self.assertEqual(gaps, {})


    def test_detect_quote_gaps_ignores_tail_after_last_bar(self):
        """股票最后一次行情之后的日子一律视为停牌，不再报缺失。"""
        from app.main import detect_quote_gaps
        trading_days = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
        quote_rows = [{"stock_id": 1, "trade_date": day} for day in trading_days[:40]]
        quote_rows += [{"stock_id": 2, "trade_date": day} for day in trading_days]
        stocks = {1: ("601059", "停牌股"), 2: ("600002", "正常股")}
        gaps = detect_quote_gaps(quote_rows, trading_days, stocks)
        self.assertEqual(gaps, {})

    def test_detect_quote_gaps_keeps_midstream_gaps(self):
        """中间的缺口（缺了又恢复）不是停牌，仍要报出。"""
        from app.main import detect_quote_gaps
        trading_days = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
        # 股票1 缺 20..29，但 30..59 有行情（恢复了）→ 中间缺口要报
        quote_rows = [{"stock_id": 1, "trade_date": day} for day in trading_days[:20]]
        quote_rows += [{"stock_id": 1, "trade_date": day} for day in trading_days[30:]]
        quote_rows += [{"stock_id": 2, "trade_date": day} for day in trading_days]
        stocks = {1: ("601059", "临时失败股"), 2: ("600002", "正常股")}
        gaps = detect_quote_gaps(quote_rows, trading_days, stocks)
        self.assertEqual(len(gaps), 10)
        self.assertEqual(gaps[trading_days[25]], [{"code": "601059", "name": "临时失败股"}])


    def test_detect_quote_gaps_flags_suspended_midgaps(self):
        """中间缺口标注 suspended：数据源该区间无数据 → 停牌，不显示。"""
        from app.main import detect_quote_gaps
        trading_days = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
        # 股票1 缺 10..19（数据源里也查不到 → 停牌），20~29 有行情
        quote_rows = [{"stock_id": 1, "trade_date": day} for day in trading_days[:10]]
        quote_rows += [{"stock_id": 1, "trade_date": day} for day in trading_days[20:]]
        quote_rows += [{"stock_id": 2, "trade_date": day} for day in trading_days]
        stocks = {1: ("001331", "胜通能源"), 2: ("600002", "正常股")}
        gaps = detect_quote_gaps(quote_rows, trading_days, stocks,
                                 source_probe=lambda code, day: False)
        self.assertEqual(gaps, {})  # 停牌中间缺口 → 不显示

    def test_detect_quote_gaps_keeps_sync_failures_midgaps(self):
        """中间缺口但数据源有数据 → 同步临时失败，仍显示。"""
        from app.main import detect_quote_gaps
        trading_days = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
        quote_rows = [{"stock_id": 1, "trade_date": day} for day in trading_days[:10]]
        quote_rows += [{"stock_id": 1, "trade_date": day} for day in trading_days[20:]]
        quote_rows += [{"stock_id": 2, "trade_date": day} for day in trading_days]
        stocks = {1: ("300376", "易联众"), 2: ("600002", "正常股")}
        # 数据源除 1-15 外都有数据 → 1-15 停牌不报，其余 9 天报出
        gaps = detect_quote_gaps(quote_rows, trading_days, stocks,
                                 source_probe=lambda code, day: day != trading_days[15])
        self.assertEqual(len(gaps), 9)
        self.assertNotIn(trading_days[15], gaps)


    @patch("app.main._fetch_quotes_with_retry")
    def test_probe_suspensions_reports_progress(self, fetch):
        """停牌探测逐只进行，并通过 progress 回调汇报 (已完成, 总数)。"""
        from app.main import _probe_suspensions
        fetch.return_value = []
        trading_days = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        quote_rows = [{"stock_id": 1, "trade_date": trading_days[0]},
                      {"stock_id": 1, "trade_date": trading_days[4]},
                      {"stock_id": 2, "trade_date": trading_days[0]},
                      {"stock_id": 2, "trade_date": trading_days[4]}]
        stocks = {1: ("001331", "甲"), 2: ("300376", "乙")}
        progress = []
        result = _probe_suspensions(quote_rows, trading_days, stocks,
                                    progress=lambda done, total: progress.append((done, total)))
        self.assertEqual(progress, [(1, 2), (2, 2)])
        # fetch 返回空 → 两只股票的中间缺口都判为停牌
        self.assertEqual(result, {"001331": {trading_days[1], trading_days[2], trading_days[3]},
                                  "300376": {trading_days[1], trading_days[2], trading_days[3]}})

    @patch("app.main._fetch_quotes_with_retry")
    def test_probe_suspensions_marks_provider_missing_days(self, fetch):
        """数据源有部分日子 → 只有数据源也缺的日子才判停牌。"""
        from app.main import _probe_suspensions
        trading_days = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        quote_rows = [{"stock_id": 1, "trade_date": trading_days[0]},
                      {"stock_id": 1, "trade_date": trading_days[4]}]
        stocks = {1: ("300376", "易联众")}
        # 数据源缺 1-2、1-4，但有 1-3 → 1-3 不是停牌（同步失败），其余是
        fetch.return_value = [{"date": trading_days[2].isoformat()}]
        result = _probe_suspensions(quote_rows, trading_days, stocks)
        self.assertEqual(result["300376"], {trading_days[1], trading_days[3]})

    @patch("app.main._fetch_quotes_with_retry")
    def test_probe_suspensions_skips_stocks_without_midgaps(self, fetch):
        """没有中间缺口的股票不探测（不浪费请求）。"""
        from app.main import _probe_suspensions
        trading_days = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        quote_rows = [{"stock_id": 1, "trade_date": day} for day in trading_days]
        stocks = {1: ("600519", "贵州茅台")}
        result = _probe_suspensions(quote_rows, trading_days, stocks)
        fetch.assert_not_called()
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
