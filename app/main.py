import csv
import io
import json
import logging
import re
import threading
import time as _time_module
import unicodedata
import urllib.parse
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session
from .analyzer import (AUTO_KEYWORD_MIN_SAMPLES, AUTO_KEYWORD_CATEGORY, MANUAL_KEYWORD_CATEGORY,
                       discover_auto_keywords, discovered_keyword_detail, discovered_keyword_stats,
                       ensure_manual_keywords, extract_topic, keyword_stats, rebuild_returns)
from .db import SessionLocal, init_db
from .market import fetch_akshare_quotes, fetch_akshare_stock_master
from .models import (Keyword, PlanetCircle, PlanetTopic, Stock, StockDailyQuote,
                     StockEventReturn, SyncJob, TopicKeyword, TopicStock)
from .schemas import (KeywordStat, ManualKeywordActiveIn, ManualKeywordIn, QuoteSyncIn,
                      TopicAnnotationsIn, TopicImportResult, TopicIn, ZsxqSyncIn, ZsxqSyncResult)
from .zzr import calculate_zzr

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("planet-stock")

BJT = ZoneInfo("Asia/Shanghai")
# A-share market closes at 15:00 Beijing time; daily bars returned before
# that are intraday snapshots, not final OHLCV.
MARKET_CLOSE_CUTOFF = time(15, 10)


def _market_closed_today() -> bool:
    """True when today's (Beijing time) final daily bar is safe to store."""
    return datetime.now(BJT).timetz() >= MARKET_CLOSE_CUTOFF


@asynccontextmanager
async def lifespan(_app):
    init_db()
    db = SessionLocal()
    try:
        ensure_manual_keywords(db)
    finally:
        db.close()
    yield


app = FastAPI(title="知识星球观点分析", version="0.1.0", lifespan=lifespan)

KEYWORD_SPLIT_RE = re.compile(r"[\s,，]+")
MARKETS = {"all", "main", "gem", "star", "bse"}
PERIODS = {"1d", "3d", "5d", "10d", "20d", "60d", "mtd", "ytd"}


def market_for_code(code: str) -> str:
    code = (code or "").strip().upper()
    if code.startswith(("300", "301")):
        return "创业板"
    if code.startswith("688"):
        return "科创板"
    if code.startswith(("4", "8", "920")):
        return "北交所"
    if code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
        return "主板"
    return "其他"


def market_clause(column, market: str):
    if market == "all":
        return None
    prefixes = {
        "main": ("600%", "601%", "603%", "605%", "000%", "001%", "002%", "003%"),
        "gem": ("300%", "301%"),
        "star": ("688%",),
        "bse": ("4%", "8%", "920%"),
    }.get(market)
    return or_(*(column.like(prefix) for prefix in prefixes))


def split_keywords(value: str) -> list[str]:
    return list(dict.fromkeys(term for term in KEYWORD_SPLIT_RE.split(value.strip()) if term))


def has_real_content(title: str, content: str) -> bool:
    """Reject attachment-only topics: after decoding URL-encoded #标签# markers
    and stripping them, the text must still contain readable characters,
    otherwise it is a file/db entry whose body cannot be read in this app."""
    text = urllib.parse.unquote(f"{title} {content}")
    text = re.sub(r"#[^#]{0,60}#", " ", text)
    text = text.replace("#", " ")
    text = re.sub(r"\s+", "", text)
    return bool(re.search(r"[一-鿿A-Za-z0-9]", text))


def normalize_manual_keyword(value: str) -> str:
    term = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value or "").strip())
    if not term:
        raise HTTPException(400, "keyword cannot be empty")
    return term


def manual_keyword_item(keyword: Keyword, topic_counts: dict[int, int]) -> dict:
    return {"id": keyword.id, "keyword": keyword.keyword, "active": keyword.active,
            "topic_count": topic_counts.get(keyword.id, 0)}


def topic_summary(topic: PlanetTopic) -> dict:
    return {"topic_id": topic.topic_id, "title": topic.title, "content": topic.content,
            "author": topic.author, "published_at": topic.published_at, "source_url": topic.source_url,
            "tags": json.loads(topic.tags or "[]")}


def latest_quote_checkpoint(data_dir: Path = Path("/data")) -> dict | None:
    """Read the newest valid full-market quote checkpoint."""
    candidates = sorted(data_dir.glob("quote-sync-all-*.json"),
                        key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(state, dict) or not isinstance(state.get("completed_codes"), list):
            continue
        state = dict(state)
        state["state_file"] = path.name
        state["updated_at"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        return state
    return None


# In-container resume worker: reads the newest /data checkpoint and continues
# the full-market sync in small batches, so the web page can restart the run
# without a host terminal command.
_quote_sync_lock = threading.Lock()


def _save_quote_checkpoint(path: Path, state: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _fetch_quotes_with_retry(stock_code: str, start_date: date, end_date: date,
                             adjust_type: str, max_attempts: int = 3) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fetch_akshare_quotes(stock_code, start_date, end_date, adjust_type)
        except Exception as exc:
            last_error = exc
            log.warning("quote fetch failed code=%s attempt=%d/%d error=%s",
                        stock_code, attempt, max_attempts, type(exc).__name__)
            if attempt < max_attempts:
                _time_module.sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise last_error


def _store_quote_rows(db: Session, stock_code: str, rows: list[dict], adjust_type: str) -> dict[str, int]:
    stock = db.scalar(select(Stock).where(Stock.stock_code == stock_code))
    if not stock:
        stock = Stock(stock_code=stock_code, exchange="SH" if stock_code.startswith("6") else "SZ")
        db.add(stock)
        db.flush()

    prepared: list[tuple[date, dict]] = []
    invalid = 0
    today = date.today()
    for row in rows:
        try:
            trade_date = date.fromisoformat(row["date"])
        except (KeyError, TypeError, ValueError):
            invalid += 1
            continue
        # Never store today's bar before the market closes: it would be an
        # intraday snapshot that later syncs would skip as "already exists".
        if trade_date >= today and not _market_closed_today():
            continue
        prepared.append((trade_date, row))
    existing = set(db.scalars(select(StockDailyQuote.trade_date).where(
        StockDailyQuote.stock_id == stock.id,
        StockDailyQuote.adjust_type == adjust_type,
        StockDailyQuote.trade_date.in_([item[0] for item in prepared]),
    ))) if prepared else set()
    imported = skipped = 0
    for trade_date, row in prepared:
        if trade_date in existing:
            # Allow re-sync to refresh today's earlier intraday bar once the
            # market has closed; historical rows are never rewritten.
            if trade_date < today:
                skipped += 1
                continue
            db.execute(delete(StockDailyQuote).where(
                StockDailyQuote.stock_id == stock.id,
                StockDailyQuote.adjust_type == adjust_type,
                StockDailyQuote.trade_date == trade_date))
        db.add(StockDailyQuote(stock_id=stock.id, trade_date=trade_date,
                               open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                               volume=row["volume"], amount=row["amount"],
                               turnover_rate=row["turnover_rate"], adjust_type=adjust_type))
        existing.add(trade_date)
        imported += 1
    return {"imported": imported, "skipped": skipped, "invalid_rows": invalid}


ST_NAME_RE = re.compile(r"(?i)^\*?ST|退市")


def is_st_stock(name: str | None) -> bool:
    """ST/*ST/退市整理 stocks: suspended or delisted for long stretches; exclude
    them from quote syncing and gap detection to avoid permanent false gaps."""
    return bool(ST_NAME_RE.search(name or ""))


def is_cdr_stock(code: str) -> bool:
    """68900x codes are CDRs (存托凭证, e.g. 九号公司); the Eastmoney K-line API
    has no bars for them, so they would pollute gap reports forever."""
    return (code or "").startswith("68900")


def _missing_trade_dates_for_stock(quote_rows: list[dict], trading_days: list[date]) -> list[date]:
    """Trading days a stock is missing, ignoring days before its first bar (e.g. listing)."""
    quote_dates = {row["trade_date"] for row in quote_rows}
    if not quote_dates:
        return list(trading_days)
    first = min(quote_dates)
    return [day for day in trading_days if day >= first and day not in quote_dates]


SUSPENDED_TAIL_DAYS = 1  # any days after a stock's last bar are treated as suspension


def detect_quote_gaps(quote_rows: list[dict], trading_days: list[date],
                      stocks: dict[int, tuple[str, str]], threshold: float = 0.8,
                      source_probe=None) -> dict[date, list[dict]]:
    """Find quote gaps: for every trading day, list each stock missing a qfq bar
    (ignoring days before a stock's first bar, e.g. listings). Returns
    {date: [{code, name}, ...]}; days with no missing stocks are omitted.

    `source_probe(code, day)` optionally asks the data provider whether a bar
    exists for that stock/day: gaps the provider also lacks are suspensions and
    are excluded; gaps the provider has are sync failures worth reporting."""
    if not trading_days or not stocks:
        return {}
    stocks = {stock_id: (code, name) for stock_id, (code, name) in stocks.items()
              if not is_st_stock(name) and not is_cdr_stock(code)}
    ids_by_day: dict[date, set[int]] = {day: set() for day in trading_days}
    dates_by_stock: dict[int, set[date]] = {}
    for row in quote_rows:
        trade_date = row["trade_date"]
        if trade_date in ids_by_day:
            ids_by_day[trade_date].add(row["stock_id"])
        dates_by_stock.setdefault(row["stock_id"], set()).add(trade_date)
    gaps: dict[date, list[dict]] = {}
    day_index = {day: index for index, day in enumerate(trading_days)}
    for day, ids in ids_by_day.items():
        missing = []
        for stock_id in sorted(set(stocks) - ids):
            quote_dates = dates_by_stock.get(stock_id)
            # Skip days before a stock's first bar (e.g. not yet listed).
            if quote_dates is not None and (not quote_dates or day < min(quote_dates)):
                continue
            # Days after a stock's last bar: suspension or delisting by
            # construction — the provider has nothing there to backfill.
            if quote_dates and day > max(quote_dates) \
                    and day_index[trading_days[-1]] - day_index[max(quote_dates)] >= SUSPENDED_TAIL_DAYS - 1:
                continue
            code, name = stocks[stock_id]
            if source_probe is not None and not source_probe(code, day):
                continue  # mid-gap the provider also lacks → suspension
            missing.append({"code": code, "name": name})
        if missing:
            gaps[day] = missing
    return gaps


def _trading_days_from_rows(rows: list[dict], start: date, end: date) -> list[date]:
    """Parse AKShare trade-calendar rows into sorted dates within [start, end]."""
    days: list[date] = []
    for row in rows:
        value = str(row.get("trade_date") or row.get("交易日") or row.get("calendarDate") or "").strip()[:10]
        try:
            trade_date = date.fromisoformat(value)
        except ValueError:
            continue
        if start <= trade_date <= end:
            days.append(trade_date)
    return sorted(set(days))


def _quote_gap_sync_worker(db: Session, state: dict, progress_path: Path | None = None,
                           max_attempts: int = 3):
    """Backfill quotes for the missing (code, dates) targets in `state`.

    `targets` maps ISO dates to [{code, name}]; each unique code is fetched once
    over the span of its missing days. Stocks that still fail get their missing
    dates recorded per-day in state["failures"]."""
    state.setdefault("failures", [])
    state.setdefault("suspended", [])
    failures: dict[str, dict] = {}
    suspended: dict[str, dict] = {}
    targets: dict[str, list[str]] = {}
    for day_text, entries in (state.get("targets") or {}).items():
        for entry in entries:
            targets.setdefault(entry["code"], []).append(day_text)
    total = len(targets)
    state["total"], state["completed"] = total, 0
    state["status"] = "running"
    for index, (code, day_texts) in enumerate(sorted(targets.items()), start=1):
        name = next((e["name"] for entries in (state.get("targets") or {}).values()
                     for e in entries if e["code"] == code), code)
        start_date = date.fromisoformat(min(day_texts))
        end_date = date.fromisoformat(max(day_texts))
        try:
            rows = _fetch_quotes_with_retry(code, start_date, end_date, "qfq", max_attempts)
            if not rows:
                # No bars in the whole window: the stock was suspended on those
                # days — expected, not a failure.
                suspended[code] = {"code": code, "name": name, "dates": sorted(day_texts)}
            else:
                _store_quote_rows(db, code, rows, "qfq")
            db.commit()
            state["completed"] = index
        except Exception as exc:
            db.rollback()
            failures[code] = {"code": code, "name": name, "dates": sorted(day_texts),
                              "reason": type(exc).__name__}
        state["failures"] = list(failures.values())
        state["suspended"] = list(suspended.values())
        if progress_path is not None:
            try:
                _save_quote_checkpoint(progress_path, state)
            except OSError:
                pass
    state["status"] = "completed_with_failures" if failures else "completed"
    state["finished_at"] = datetime.now(timezone.utc).isoformat()
    if progress_path is not None:
        try:
            _save_quote_checkpoint(progress_path, state)
        except OSError:
            pass
    log.info("quote gap sync finished status=%s total=%d failed=%d",
             state["status"], total, len(failures))


def _validate_gap_range(start: date, end: date) -> tuple[date, date]:
    """Validate a manual gap-sync range; today's bar may only be stored after
    the market close cutoff (see the close-guard memory)."""
    if start > end:
        raise HTTPException(400, "start_date 不能晚于 end_date")
    today = date.today()
    if end > today:
        raise HTTPException(400, "end_date 不能晚于今天")
    if end == today and not _market_closed_today():
        raise HTTPException(400, "今天尚未收盘（15:10 前禁止同步当日行情），请选择昨天及以前的日期")
    return start, end


def _default_gap_range() -> tuple[date, date]:
    """Default gap-sync window: this year start through today, but exclude today
    itself while the market hasn't closed (its bars don't exist yet)."""
    today = date.today()
    end = today if _market_closed_today() else today - timedelta(days=1)
    return date(today.year, 1, 1), end


def _resume_quote_sync_worker(state_file: str, start_date: str, end_date: str,
                              batch_size: int = 10, max_batches: int = 600, max_attempts: int = 3):
    db = SessionLocal()
    path = Path("/data") / state_file
    state: dict = {}
    try:
        if not path.exists():
            return
        state = json.loads(path.read_text(encoding="utf-8"))
        completed = set(state.get("completed_codes") or [])
        all_stocks = list(db.execute(
            select(Stock.stock_code, Stock.stock_name).order_by(Stock.stock_code)).all())
        codes = [code for code, name in all_stocks
                 if name and not is_st_stock(name) and not is_cdr_stock(code)]
        state["total"] = len(codes)
        state["start_date"], state["end_date"], state["all_stocks"] = start_date, end_date, True
        state["status"] = "running"
        state["started_at"] = datetime.now(timezone.utc).isoformat()
        state["analysis_status"] = "pending"
        _save_quote_checkpoint(path, state)
        failed_codes: set[str] = set()
        no_data_codes: set[str] = set()
        failure_reasons: dict[str, str] = {}
        imported = skipped = no_data = invalid_rows = batches = 0
        log.info("quote resume started state_file=%s completed=%d total=%d",
                 state_file, len(completed), len(codes))
        pending = [code for code in codes if code not in completed]
        for index in range(0, min(len(pending), batch_size * max_batches), batch_size):
            batch = pending[index:index + batch_size]
            for code in batch:
                try:
                    rows = _fetch_quotes_with_retry(code, date.fromisoformat(start_date),
                                                    date.fromisoformat(end_date), "qfq", max_attempts)
                except Exception as exc:
                    failed_codes.add(code)
                    failure_reasons[code] = type(exc).__name__
                    continue
                if not rows:
                    no_data += 1
                    no_data_codes.add(code)
                    # Suspension or no bars yet: nothing to fetch later, so
                    # counting it as unattempted would keep the task paused
                    # forever — mark it completed like any other finished code.
                    completed.add(code)
                    continue
                counts = _store_quote_rows(db, code, rows, "qfq") if rows else {
                    "imported": 0, "skipped": 0, "invalid_rows": 0,
                }
                imported += counts["imported"]
                skipped += counts["skipped"]
                invalid_rows += counts["invalid_rows"]
                completed.add(code)
            db.commit()
            batches += 1
            state["completed_codes"] = sorted(completed)
            state["last_failed_codes"] = sorted(failed_codes)
            state["no_data_codes"] = sorted(no_data_codes)
            state["failure_reasons"] = failure_reasons
            state["imported"], state["skipped"] = imported, skipped
            state["no_data"], state["invalid_rows"] = no_data, invalid_rows
            _save_quote_checkpoint(path, state)
            if batches % 10 == 0:
                log.info("quote resume progress completed=%d total=%d", len(completed), len(codes))

        unattempted = set(codes) - completed - failed_codes
        if unattempted:
            state["status"] = "paused"
        else:
            state["status"] = "analyzing"
            state["analysis_status"] = "running"
            _save_quote_checkpoint(path, state)
            rebuild_returns(db)
            db.commit()
            state["analysis_status"] = "completed"
            state["analysis_event_returns"] = db.scalar(
                select(func.count()).select_from(StockEventReturn)) or 0
            state["analysis_completed_at"] = datetime.now(timezone.utc).isoformat()
            state["status"] = "completed_with_failures" if failed_codes else "completed"
            state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _save_quote_checkpoint(path, state)
        log.info("quote resume finished status=%s completed=%d total=%d failed=%d",
                 state["status"], len(completed), len(codes), len(failed_codes))
    except Exception as exc:
        db.rollback()
        if state:
            state["status"] = "failed"
            state["error"] = type(exc).__name__
            if state.get("analysis_status") == "running":
                state["analysis_status"] = "failed"
            try:
                _save_quote_checkpoint(path, state)
            except OSError:
                pass
        log.exception("quote resume worker crashed")
    finally:
        db.close()
        if _quote_sync_lock.locked():
            _quote_sync_lock.release()


@app.get("/", include_in_schema=False)
def home():
    return HTMLResponse(r"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'><title>知识星球股票观点分析</title>
    <style>
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:32px auto;max-width:1180px;color:#17233b;background:#f7f9fc}h1,h2{margin:0 0 14px}.card{background:#fff;border:1px solid #e5eaf2;border-radius:12px;padding:22px;margin:18px 0;box-shadow:0 1px 2px #dfe6f033}.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}input{padding:10px;border:1px solid #cbd5e1;border-radius:7px;font-size:14px}input[type=text]{min-width:280px}button,.button{padding:10px 15px;border:0;border-radius:7px;background:#1677ff;color:#fff;cursor:pointer;font-size:14px}button.secondary{background:#e8eef8;color:#29415f}.muted{color:#64748b;font-size:13px}.progress{height:10px;background:#e8eef8;border-radius:8px;overflow:hidden;margin:14px 0}.progress>span{display:block;height:100%;background:#1677ff}.summary{display:flex;gap:28px;flex-wrap:wrap}.summary strong{display:block;font-size:22px;margin-bottom:4px}.topic{padding:16px 0;border-bottom:1px solid #edf1f5}.topic:last-child{border-bottom:0}.topic-title{font-size:16px;font-weight:650;color:#17233b;cursor:pointer}.topic-title:hover{color:#1677ff}.meta{font-size:13px;color:#64748b;margin:7px 0}.preview{white-space:pre-wrap;line-height:1.6;color:#334155}.tag{display:inline-block;margin:3px 5px 0 0;padding:2px 7px;border-radius:12px;background:#e9f2ff;color:#2769b6;font-size:12px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;border-bottom:1px solid #e8edf3;text-align:left}.pager{display:flex;gap:10px;align-items:center;margin-top:16px}dialog{border:0;border-radius:12px;width:min(780px,90vw);max-height:82vh;box-shadow:0 16px 50px #17233b66;padding:0}dialog::backdrop{background:#17233b77}.modal-head{display:flex;justify-content:space-between;gap:20px;padding:20px 22px;border-bottom:1px solid #e8edf3}.modal-body{padding:20px 22px;white-space:pre-wrap;line-height:1.7;overflow:auto;max-height:62vh}.close{background:transparent;color:#475569;font-size:22px;padding:0}.details{margin-top:16px;padding-top:12px;border-top:1px solid #e8edf3}.error{color:#c2410c}</style>
    <body><h1>知识星球股票观点分析</h1><p class='muted'>主题按知识星球发布时间倒序；数据库保存的是接口返回的发布时间，不是本地同步时间。 · <a href='/kline'>打开 K 线查询</a> · <a href='/gap'>行情缺失补漏</a></p>
    <section class='card'><h2>行情同步进度</h2><p class='muted'>任务在后台运行；单只股票失败会自动重试 3 次，任务处理完成后自动重算收益分析。</p><div class='controls'><button id='load-quote-status'>刷新进度</button><button id='resume-quote-sync'>后台继续/重试</button><span id='quote-status' class='muted'></span></div><div id='quote-progress'></div><div id='quote-summary' class='summary'></div><p id='quote-detail' class='muted'></p></section>
    <section class='card'><h2>知识星球主题查询</h2><div class='controls'><input id='topic-keywords' type='text' placeholder='包含全部关键词，例如：深信服 翻倍'><button id='search-topics'>查询</button><button id='clear-topics' class='secondary'>清空</button></div><p id='topic-status' class='muted'></p><div id='topic-list'></div><div id='pager' class='pager'></div></section>
    <section class='card'><h2>星球在线搜索</h2><p class='muted'>直接搜索知识星球服务器上的全部主题（官方接口），不限于已同步到本地的数据；默认要求所有关键词同时出现。</p><div class='controls'><input id='planet-search-input' type='text' placeholder='例如：富满微 翻倍'><select id='planet-search-mode'><option value='and'>同时包含</option><option value='or'>任一包含</option></select><button id='planet-search-go'>搜索</button></div><p id='planet-search-status' class='muted'></p><div id='planet-search-results'></div></section>
    <section class='card'><h2>关键词统计</h2><p class='muted'>“未来 1 月”指发布事件日后的 20 个交易日；只有完整取得 20 个交易日行情的样本才参与涨幅≥10%排行。</p><div class='controls'><input id='stat-keyword' placeholder='关键词'><input id='stat-stock' placeholder='股票代码'><input id='stat-from' type='date'><input id='stat-to' type='date'><button id='load-stats'>刷新统计</button><a class='button' href='/api/export/keywords.csv'>导出 CSV</a></div><table><thead><tr><th>关键词</th><th>主题数</th><th>股票数</th><th>未来1月平均收益</th><th>1月涨幅≥10%比例</th><th>有效样本</th></tr></thead><tbody id='stat-rows'></tbody></table></section>
    <section class='card'><h2>人工关键词词库</h2><p class='muted'>新增和修改的词会用于后续同步主题；停用只停止后续识别，历史关联仍保留。</p><div class='controls'><input id='manual-keyword' placeholder='例如：超预期、重点推荐'><button id='add-manual-keyword'>新增关键词</button><button id='load-manual-keywords' class='secondary'>刷新词库</button></div><p id='manual-status' class='muted'></p><table><thead><tr><th>关键词</th><th>状态</th><th>已关联主题</th><th>操作</th></tr></thead><tbody id='manual-rows'></tbody></table></section>
    <section class='card'><h2>荐股强调词分析</h2><p class='muted'>只识别“继续看好、超预期、翻倍空间、强 Call、务必重视、重点推荐”等荐股强度和上涨空间表达，不把股票名称、行业名词当作关键词。默认至少 10 个有效样本才参与排行；结果是历史相关性，不是因果关系。</p><div class='controls'><input id='auto-filter' placeholder='筛选强调词，例如：重点推荐'><button id='discover-auto'>重新扫描强调词</button><button id='load-auto' class='secondary'>刷新结果</button></div><p id='auto-status' class='muted'></p><table><thead><tr><th>强调词</th><th>出现主题</th><th>有效样本</th><th>上涨次数</th><th>上涨比例</th><th>平均20日收益</th><th>相对基准提升</th><th>按股票去重上涨率</th><th>代表股票</th></tr></thead><tbody id='auto-rows'></tbody></table></section>
    <dialog id='topic-modal'><div class='modal-head'><div><strong id='modal-title'></strong><div id='modal-meta' class='meta'></div></div><button id='close-modal' class='close' aria-label='关闭'>×</button></div><div id='modal-body' class='modal-body'></div></dialog>
    <dialog id='auto-modal'><div class='modal-head'><div><strong id='auto-modal-title'></strong><div id='auto-modal-meta' class='meta'></div></div><button id='close-auto-modal' class='close' aria-label='关闭'>×</button></div><div id='auto-modal-body' class='modal-body'></div></dialog>
    <script>
    const state={page:1,pageSize:20,quoteTimer:null}; const $=id=>document.getElementById(id);
    const bjt=value=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value));
    const add=(parent,tag,text,className='')=>{const el=document.createElement(tag);el.textContent=text;if(className)el.className=className;parent.append(el);return el};
    const tags=(parent,values)=>values.forEach(value=>add(parent,'span','#'+value,'tag'));
    const preview=value=>value.replace(/\s+/g,' ').slice(0,180)+(value.replace(/\s+/g,' ').length>180?'…':'');
    async function loadQuoteStatus(){clearTimeout(state.quoteTimer);const button=$('load-quote-status');button.disabled=true;$('quote-status').className='muted';$('quote-status').textContent='正在查询…';try{const res=await fetch('/api/quotes/sync-status');const data=await res.json();if(!res.ok)throw new Error(data.detail||'进度查询失败');$('quote-progress').replaceChildren();$('quote-summary').replaceChildren();if(!data.checkpoint){$('quote-status').textContent='尚未找到全量行情同步断点';$('quote-detail').textContent=`数据库已有 ${data.database.stocks_with_quotes} 只股票的行情，共 ${data.database.quote_rows} 条`;return}const bar=document.createElement('div');bar.className='progress';const fill=document.createElement('span');fill.style.width=data.progress.percent+'%';bar.append(fill);$('quote-progress').append(bar);[[`${data.progress.completed} / ${data.progress.total}`,'同步成功股票'],[data.database.stocks_with_quotes,'数据库已有行情股票'],[data.database.quote_rows,'数据库行情记录'],[data.progress.failed,'重试后仍失败股票']].forEach(([value,label])=>{const box=document.createElement('div');add(box,'strong',String(value));add(box,'span',label,'muted');$('quote-summary').append(box)});$('quote-status').textContent=`${data.progress.status_text}，处理 ${data.progress.percent.toFixed(2)}%`;const analysis=data.progress.analysis_status==='completed'?`；分析已自动重算（${data.progress.analysis_event_returns||0} 条事件）`:data.progress.analysis_status==='running'?'；正在自动重算分析':'';$('quote-detail').textContent=`同步范围：${data.progress.start_date||'-'} 至 ${data.progress.end_date||'-'}；数据库行情范围：${data.database.min_date||'-'} 至 ${data.database.max_date||'-'}；断点更新：${bjt(data.progress.updated_at)}${analysis}`;if(['running','analyzing'].includes(data.progress.status))state.quoteTimer=setTimeout(loadQuoteStatus,5000)}catch(error){$('quote-status').textContent=error.message;$('quote-status').className='error'}finally{button.disabled=false}}
    async function loadTopics(page=1){const q=new URLSearchParams({page:String(page),page_size:String(state.pageSize)});const keywords=$('topic-keywords').value.trim();if(keywords)q.set('keywords',keywords);const res=await fetch('/api/topics/search?'+q);const data=await res.json();if(!res.ok)throw new Error(data.detail||'查询失败');state.page=data.page;const list=$('topic-list');list.replaceChildren();$('topic-status').className='muted';$('topic-status').textContent=`共 ${data.total} 条${data.keywords.length?'；同时包含：'+data.keywords.join('、'):''}`;if(!data.items.length){add(list,'p','没有符合条件的主题。','muted')}data.items.forEach(topic=>{const row=document.createElement('article');row.className='topic';const title=add(row,'div',topic.title||'（无标题）','topic-title');title.onclick=()=>showTopic(topic.topic_id).catch(showError);add(row,'div',`${bjt(topic.published_at)} · ${topic.author||'未知作者'}`,'meta');add(row,'div',preview(topic.content),'preview');tags(row,topic.tags||[]);list.append(row)});const pager=$('pager');pager.replaceChildren();const pages=Math.max(1,Math.ceil(data.total/data.page_size));const prev=add(pager,'button','上一页','secondary');prev.disabled=data.page<=1;prev.onclick=()=>loadTopics(data.page-1).catch(showError);add(pager,'span',`第 ${data.page} / ${pages} 页`,'muted');const pageInput=document.createElement('input');pageInput.type='number';pageInput.min='1';pageInput.max=String(pages);pageInput.value=String(data.page);pageInput.style.width='64px';pageInput.setAttribute('aria-label','跳转页码');pager.append(pageInput);const jump=add(pager,'button','跳转','secondary');const go=()=>{const target=Number(pageInput.value);if(!Number.isInteger(target)||target<1||target>pages){throw new Error(`请输入 1 到 ${pages} 的页码`)}return loadTopics(target)};jump.onclick=()=>go().catch(showError);pageInput.onkeydown=event=>{if(event.key==='Enter')go().catch(showError)};const next=add(pager,'button','下一页','secondary');next.disabled=data.page>=pages;next.onclick=()=>loadTopics(data.page+1).catch(showError)}
    async function showTopic(id){const res=await fetch('/api/topics/'+encodeURIComponent(id));const topic=await res.json();if(!res.ok)throw new Error(topic.detail||'无法读取主题');$('modal-title').textContent=topic.title||'（无标题）';$('modal-meta').textContent=`知识星球发布时间：${bjt(topic.published_at)} · ${topic.author||'未知作者'}`;const body=$('modal-body');body.replaceChildren();tags(body,topic.tags||[]);add(body,'div',topic.content||'（无正文）','preview');const details=document.createElement('div');details.className='details';add(details,'div','已识别股票：'+(topic.stocks.map(x=>`${x.code} ${x.name}`.trim()).join('、')||'无'));add(details,'div','已识别关键词：'+(topic.keywords.map(x=>x.keyword).join('、')||'无'));body.append(details);$('topic-modal').showModal()}
    async function loadStats(){const q=new URLSearchParams();[['stat-keyword','keyword'],['stat-stock','stock_code'],['stat-from','start_date'],['stat-to','end_date']].forEach(([id,key])=>{const value=$(id).value;if(value)q.set(key,value)});const res=await fetch('/api/stats/keywords?'+q);const data=await res.json();const rows=$('stat-rows');rows.replaceChildren();data.forEach(item=>{const row=document.createElement('tr');[item.keyword,item.topic_count,item.stock_count,item.avg_return_20d==null?'-':(item.avg_return_20d*100).toFixed(2)+'%',item.rise_rate_10pct_20d==null?'-':(item.rise_rate_10pct_20d*100).toFixed(2)+'%',`${item.eligible_count_20d} / ${item.sample_sufficient?'充足':'不足10篇'}`].forEach(value=>add(row,'td',String(value)));rows.append(row)})}
    async function loadManualKeywords(){const res=await fetch('/api/keywords/manual?include_inactive=true');const data=await res.json();if(!res.ok)throw new Error(data.detail||'词库查询失败');const rows=$('manual-rows');rows.replaceChildren();$('manual-status').className='muted';$('manual-status').textContent=`共 ${data.length} 个关键词，停用词仍保留历史关联`;data.forEach(item=>{const row=document.createElement('tr');add(row,'td',item.keyword);add(row,'td',item.active?'启用':'停用');add(row,'td',String(item.topic_count));const actions=document.createElement('td');const edit=add(actions,'button','修改','secondary');edit.onclick=()=>editManualKeyword(item.id,item.keyword).catch(showManualError);const toggle=add(actions,'button',item.active?'停用':'启用','secondary');toggle.onclick=()=>toggleManualKeyword(item.id,!item.active).catch(showManualError);row.append(actions);rows.append(row)})}
    async function createManualKeyword(){const input=$('manual-keyword');const keyword=input.value.trim();if(!keyword){$('manual-status').textContent='请输入关键词';return}const res=await fetch('/api/keywords/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({keyword})});const data=await res.json();if(!res.ok)throw new Error(data.detail||'新增失败');input.value='';$('manual-status').textContent=`已保存“${data.keyword}”，后续同步主题时生效`;await loadManualKeywords()}
    async function editManualKeyword(id,current){const keyword=prompt('修改关键词',current);if(keyword===null||!keyword.trim()||keyword.trim()===current)return;const res=await fetch('/api/keywords/manual/'+encodeURIComponent(id),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({keyword})});const data=await res.json();if(!res.ok)throw new Error(data.detail||'修改失败');await loadManualKeywords()}
    async function toggleManualKeyword(id,active){const res=await fetch('/api/keywords/manual/'+encodeURIComponent(id),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({active})});const data=await res.json();if(!res.ok)throw new Error(data.detail||'状态更新失败');await loadManualKeywords()}
    async function loadAutoKeywords(){const q=new URLSearchParams();const filter=$('auto-filter').value.trim();if(filter)q.set('keyword',filter);const res=await fetch('/api/stats/auto-keywords?'+q);const data=await res.json();if(!res.ok)throw new Error(data.detail||'自动关键词查询失败');const ranked=data.filter(item=>item.sample_sufficient);const rows=$('auto-rows');rows.replaceChildren();$('auto-status').className='muted';$('auto-status').textContent=data.length?`共 ${data.length} 个候选，显示 ${ranked.length} 个样本充足（有效样本≥10）`:'暂无自动关键词，请点击“扫描并更新关键词”';if(!ranked.length)return;ranked.forEach(item=>{const row=document.createElement('tr');const word=document.createElement('button');word.className='secondary';word.textContent=item.keyword;word.onclick=()=>showAutoKeyword(item.keyword_id).catch(showAutoError);const wordCell=document.createElement('td');wordCell.append(word);row.append(wordCell);[item.topic_count,item.eligible_count_20d,item.success_count_20d,item.rise_rate_10pct_20d==null?'-':(item.rise_rate_10pct_20d*100).toFixed(2)+'%',item.avg_return_20d==null?'-':(item.avg_return_20d*100).toFixed(2)+'%',item.uplift_vs_baseline==null?'-':(item.uplift_vs_baseline*100).toFixed(2)+'%',item.dedup_stock_rise_rate_20d==null?'-':(item.dedup_stock_rise_rate_20d*100).toFixed(2)+'%（'+item.dedup_stock_count+'只）',item.representative_stocks.map(x=>`${x.name||x.code}(${x.topics})`).join('、')||'-'].forEach(value=>add(row,'td',String(value)));rows.append(row)})}
    async function discoverAutoKeywords(){const button=$('discover-auto');button.disabled=true;$('auto-status').className='muted';$('auto-status').textContent='正在清理旧结果并扫描荐股强调词，数据量较大时需要一些时间…';try{const res=await fetch('/api/analyze/discover-keywords',{method:'POST'});const data=await res.json();if(!res.ok)throw new Error(data.detail||'强调词扫描失败');$('auto-status').textContent=`扫描 ${data.topics_scanned} 篇主题，发现 ${data.candidate_keywords} 个强调词，建立 ${data.links_added} 条关联`;await loadAutoKeywords()}finally{button.disabled=false}}
    async function showAutoKeyword(id){const res=await fetch('/api/stats/auto-keywords/'+encodeURIComponent(id));const data=await res.json();if(!res.ok)throw new Error(data.detail||'无法读取关键词详情');$('auto-modal-title').textContent=`关键词：${data.keyword}`;$('auto-modal-meta').textContent=`展示最近 ${data.topics.length} 篇相关主题`;$('auto-modal-body').replaceChildren();if(!data.topics.length){add($('auto-modal-body'),'p','没有相关主题。','muted')}data.topics.forEach(topic=>{const block=document.createElement('article');block.className='topic';add(block,'div',topic.title||'（无标题）','topic-title');add(block,'div',`${bjt(topic.published_at)} · ${topic.author||'未知作者'}`,'meta');add(block,'div','出现上下文：'+(topic.context||'—'),'preview');add(block,'div','关联股票：'+(topic.stocks.map(x=>`${x.code} ${x.name}`.trim()).join('、')||'无'),'meta');const returns=topic.returns.map(x=>`${x.code} 20日收益 ${x.return_20d==null?'-':(x.return_20d*100).toFixed(2)+'%'}，最高收盘 ${x.max_return_20d==null?'-':(x.max_return_20d*100).toFixed(2)+'%'}`).join('；');add(block,'div','行情结果：'+(returns||'无完整20日行情'),'meta');$('auto-modal-body').append(block)});$('auto-modal').showModal()}
    async function resumeQuoteSync(){const button=$('resume-quote-sync');button.disabled=true;$('quote-status').className='muted';$('quote-status').textContent='正在启动后台任务…';try{const res=await fetch('/api/quotes/sync-resume',{method:'POST'});const data=await res.json();if(!res.ok)throw new Error(data.detail||'启动失败');$('quote-status').textContent=`后台任务已启动：已完成 ${data.completed} / ${data.total}，待重试 ${data.retrying}`;state.quoteTimer=setTimeout(loadQuoteStatus,2000)}catch(error){$('quote-status').textContent=error.message;$('quote-status').className='error'}finally{button.disabled=false}}
    async function searchPlanet(){const button=$('planet-search-go');const input=$('planet-search-input').value.trim();const list=$('planet-search-results');if(!input){$('planet-search-status').textContent='请输入搜索关键词';$('planet-search-status').className='error';return}button.disabled=true;$('planet-search-status').className='muted';$('planet-search-status').textContent='正在搜索知识星球…';list.replaceChildren();try{const q=new URLSearchParams({q:input,mode:$('planet-search-mode').value,limit:'50'});const res=await fetch('http://127.0.0.1:8765/search?'+q);const data=await res.json();if(!res.ok)throw new Error(data.error||'搜索失败');$('planet-search-status').className='muted';$('planet-search-status').textContent=data.items.length?`命中 ${data.items.length} 条主题（点击标题打开星球原文）`:'没有命中的主题，可换关键词试试。';data.items.forEach(topic=>{const row=document.createElement('article');row.className='topic';const title=add(row,'div',topic.title||'（无标题）','topic-title');title.onclick=()=>window.open(topic.url,'_blank');add(row,'div',`${bjt(topic.create_time)} · ${topic.author||'未知作者'}`,'meta');add(row,'div',preview(topic.content),'preview');list.append(row)})}catch(error){const hint=error.message==='Failed to fetch'?'搜索服务未启动：请在主机启动 zsxq_search_server.py':error.message;$('planet-search-status').textContent=hint;$('planet-search-status').className='error'}finally{button.disabled=false}}
    $('load-quote-status').onclick=loadQuoteStatus;$('planet-search-go').onclick=()=>searchPlanet().catch(showError);$('planet-search-input').onkeydown=event=>{if(event.key==='Enter')searchPlanet().catch(showError)};$('resume-quote-sync').onclick=()=>resumeQuoteSync().catch(showError);$('search-topics').onclick=()=>loadTopics(1).catch(showError);$('clear-topics').onclick=()=>{$('topic-keywords').value='';loadTopics(1).catch(showError)};$('topic-keywords').onkeydown=event=>{if(event.key==='Enter')loadTopics(1).catch(showError)};$('close-modal').onclick=()=>$('topic-modal').close();function showError(error){$('topic-status').textContent=error.message;$('topic-status').className='error'}function showAutoError(error){$('auto-status').textContent=error.message;$('auto-status').className='error'}function showManualError(error){$('manual-status').textContent=error.message;$('manual-status').className='error'}$('load-stats').onclick=()=>loadStats().catch(showError);$('discover-auto').onclick=()=>discoverAutoKeywords().catch(showAutoError);$('load-auto').onclick=()=>loadAutoKeywords().catch(showAutoError);$('auto-filter').onkeydown=event=>{if(event.key==='Enter')loadAutoKeywords().catch(showAutoError)};$('add-manual-keyword').onclick=()=>createManualKeyword().catch(showManualError);$('load-manual-keywords').onclick=()=>loadManualKeywords().catch(showManualError);$('manual-keyword').onkeydown=event=>{if(event.key==='Enter')createManualKeyword().catch(showManualError)};$('close-auto-modal').onclick=()=>$('auto-modal').close();loadQuoteStatus();loadTopics().catch(showError);loadStats().catch(showError);loadManualKeywords().catch(showManualError);loadAutoKeywords().catch(showAutoError);
    </script></body></html>""")


@app.get("/gap", include_in_schema=False)
def gap_page():
    """Quote-gap report page: missing trading days/stocks, one-click backfill."""
    return HTMLResponse(r"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'><title>行情缺失补漏</title>
    <style>
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:32px auto;max-width:1180px;color:#17233b;background:#f7f9fc}h1{margin:0 0 14px}.card{background:#fff;border:1px solid #e5eaf2;border-radius:12px;padding:22px;margin:18px 0;box-shadow:0 1px 2px #dfe6f033}.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}input{padding:10px;border:1px solid #cbd5e1;border-radius:7px;font-size:14px}button{padding:10px 15px;border:0;border-radius:7px;background:#1677ff;color:#fff;cursor:pointer;font-size:14px}button.secondary{background:#e8eef8;color:#29415f}button:disabled{opacity:.5;cursor:default}.muted{color:#64748b;font-size:13px}.error{color:#c2410c}.summary{display:flex;gap:28px;flex-wrap:wrap}.summary strong{display:block;font-size:22px;margin-bottom:4px}.progress{height:10px;background:#e8eef8;border-radius:8px;overflow:hidden;margin:14px 0}.progress>span{display:block;height:100%;background:#1677ff}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;border-bottom:1px solid #e8edf3;text-align:left;vertical-align:top}.day{font-weight:650}.fail{color:#c2410c}details{margin:6px 0}summary{cursor:pointer;font-weight:650}
    </style>
    <body><h1>行情缺失补漏</h1>
    <p class='muted'>检测今年以来各交易日 qfq 行情覆盖情况（首根 K 线之前的交易日视为未上市，不算缺失）。<a href='/'>返回首页</a></p>
    <section class='card'>
      <h2>缺失概览</h2>
      <div class='controls'><button id='load-gap'>重新检测缺失</button><button id='sync-gap'>同步缺失行情</button><span id='gap-status' class='muted'></span></div>
      <div class='controls' style='margin-top:10px'><input id='from-date' type='date'><input id='to-date' type='date'><span class='muted'>（可选）只同步该时间段内的缺失</span></div>
      <div id='gap-progress'></div>
      <div id='gap-summary' class='summary'></div>
      <p id='gap-status-detail' class='muted'></p>
      <div id='gap-sync-result'></div>
    </section>
    <section class='card'><h2>缺失明细（按交易日）</h2><p id='gap-detail' class='muted'>点击「重新检测缺失」加载。</p><div id='gap-table'></div></section>
    <script>
    const $=id=>document.getElementById(id);
    let gapData=null, timer=null;
    const add=(parent,tag,text,className='')=>{const el=document.createElement(tag);el.textContent=text;if(className)el.className=className;parent.append(el);return el};
    async function loadGap(){const button=$('load-gap');button.disabled=true;$('gap-status').className='muted';$('gap-status').textContent='正在启动检测…';try{await fetch('/api/quotes/gap/detect',{method:'POST'});pollGapDetect()}catch(error){$('gap-status').textContent=error.message;$('gap-status').className='error'}finally{button.disabled=false}}
    let detectPoll=null;
    async function pollGapDetect(){clearTimeout(detectPoll);try{const res=await fetch('/api/quotes/gap');const data=await res.json();if(!res.ok)throw new Error(data.detail||'检测失败');if(data.status==='running'||data.status==='started'){const p=data.progress||{};$('gap-status').textContent=`正在检测：已探测 ${p.done||0} / ${p.total||'?'} 只股票（探测数据源判断停牌，需要一分钟左右）…`;if(p.total){$('gap-progress').replaceChildren();const bar=document.createElement('div');bar.className='progress';const fill=document.createElement('span');fill.style.width=Math.round((p.done||0)*100/p.total)+'%';bar.append(fill);$('gap-progress').append(bar)}detectPoll=setTimeout(pollGapDetect,3000);return}if(data.status==='error'){throw new Error('检测失败，请重试')}$('gap-progress').replaceChildren();gapData=data;$('gap-status').textContent='检测完成。';$('gap-detail').textContent=data.total_missing?('共 '+data.total_missing+' 条缺失（股票×交易日）'):'未检测到缺失行情，无需补漏。';renderGap(data)}catch(error){$('gap-status').textContent=error.message;$('gap-status').className='error'}}
    function renderGap(data){const table=$('gap-table');table.replaceChildren();const days=Object.keys(data.gaps||{}).sort().reverse();if(!days.length)return;const t=document.createElement('table');const head=document.createElement('tr');['交易日','缺失股票数','股票明细'].forEach(x=>add(head,'th',x));t.append(head);days.forEach(day=>{const row=document.createElement('tr');add(row,'td',day,'day');add(row,'td',String(data.gaps[day].length));const cell=document.createElement('td');const names=data.gaps[day].map(x=>x.code+' '+(x.name||''));if(names.length>30){const d=document.createElement('details');add(d,'summary',names.slice(0,30).join('、')+' … 共'+names.length+'只');add(d,'div',names.join('、'));cell.append(d)}else{add(cell,'span',names.join('、'))}row.append(cell);t.append(row)});table.append(t)}
    async function syncGap(){if(!confirm('确定开始补同步缺失行情？任务在后台运行。'))return;const button=$('sync-gap');button.disabled=true;$('gap-status').className='muted';$('gap-status').textContent='正在启动…';const q=new URLSearchParams();const from=$('from-date').value,to=$('to-date').value;if(from)q.set('start_date',from);if(to)q.set('end_date',to);try{const res=await fetch('/api/quotes/gap-sync?'+q,{method:'POST'});const data=await res.json();if(!res.ok)throw new Error(data.detail||'启动失败');if(data.status==='no_gaps'){$('gap-status').textContent=data.message;return}$('gap-status').textContent=`补漏已启动：${data.days} 个缺失日、${data.stocks} 只股票`;timer=setTimeout(pollGapStatus,2000)}catch(error){$('gap-status').textContent=error.message;$('gap-status').className='error'}finally{button.disabled=false}}
    async function pollGapStatus(){clearTimeout(timer);try{const res=await fetch('/api/quotes/gap-sync-status');const data=await res.json();$('gap-progress').replaceChildren();if(data.status==='running'||data.status==='pending'){const bar=document.createElement('div');bar.className='progress';const fill=document.createElement('span');const total=data.total||1;fill.style.width=Math.round((data.completed||0)*100/total)+'%';bar.append(fill);$('gap-progress').append(bar);$('gap-status-detail').textContent=`补漏中：${data.completed||0} / ${total} 只股票已完成`;timer=setTimeout(pollGapStatus,4000)}else if(data.status==='completed'||data.status==='completed_with_failures'){renderGapResult(data)}else if(data.status!=='idle'){$('gap-status-detail').textContent='状态：'+data.status}}catch(error){$('gap-status-detail').textContent=error.message;$('gap-status-detail').className='error'}}
    function renderGapResult(data){const box=$('gap-sync-result');box.replaceChildren();const failures=data.failures||[];if(!failures.length){add(box,'p','✅ 补漏完成，全部成功。');return}add(box,'p','⚠️ 补漏完成，'+failures.length+' 只股票仍失败：');const t=document.createElement('table');const head=document.createElement('tr');['股票','失败日期','原因'].forEach(x=>add(head,'th',x));t.append(head);failures.forEach(item=>{const row=document.createElement('tr');add(row,'td',item.code+' '+(item.name||''));add(row,'td',(item.dates||[]).join('、'),'fail');add(row,'td',item.reason||'','fail');t.append(row)});box.append(t)}
    $('load-gap').onclick=()=>loadGap().catch(()=>{});$('sync-gap').onclick=()=>syncGap().catch(()=>{});loadGap().catch(()=>{});pollGapStatus();
    </script></body></html>""")


@app.get("/kline", include_in_schema=False)
def kline_page():
    """Serve the standalone Lightweight Charts stock history page."""
    html_path = Path(__file__).with_name("kline.html")
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}


def _topic_data(item: TopicIn) -> dict:
    data = item.model_dump()
    published_at = data["published_at"]
    data["published_at"] = (published_at.replace(tzinfo=timezone.utc) if published_at.tzinfo is None
                             else published_at.astimezone(timezone.utc))
    data["tags"] = json.dumps(data.get("tags") or [], ensure_ascii=False)
    return data


@app.post("/api/topics/import", response_model=TopicImportResult)
def import_topics(topics: list[TopicIn], db: Session = Depends(db_session)):
    imported = skipped = 0
    stock_catalog = list(db.scalars(select(Stock).where(Stock.stock_name != "")))
    for item in topics:
        if db.scalar(select(PlanetTopic).where(PlanetTopic.topic_id == item.topic_id)):
            skipped += 1
            continue
        topic = PlanetTopic(**_topic_data(item))
        db.add(topic); db.flush()
        extract_topic(db, topic, stock_catalog)
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped}


@app.post("/api/topics/sync/zsxq", response_model=ZsxqSyncResult)
def sync_zsxq(request: ZsxqSyncIn, db: Session = Depends(db_session)):
    """Receive normalized read-only topics from the host zsxq-cli script."""
    if request.scope not in {"digests", "all"}:
        raise HTTPException(400, "scope must be digests or all")
    if len(request.topics) > 1000:
        raise HTTPException(400, "at most 1000 topics per sync")
    job = SyncJob(status="running")
    db.add(job)
    db.commit()
    imported = skipped = 0
    try:
        circle = db.scalar(select(PlanetCircle).where(PlanetCircle.circle_id == request.group_id))
        if not circle:
            circle = PlanetCircle(circle_id=request.group_id, name=request.group_name or request.group_id)
            db.add(circle)
        elif request.group_name:
            circle.name = request.group_name
        stock_catalog = list(db.scalars(select(Stock).where(Stock.stock_name != "")))
        for item in request.topics:
            if db.scalar(select(PlanetTopic).where(PlanetTopic.topic_id == item.topic_id)):
                skipped += 1
                continue
            if not has_real_content(item.title, item.content):
                skipped += 1
                log.info("topic skipped (attachment-only): %s %s", item.topic_id, item.title[:40])
                continue
            topic = PlanetTopic(**_topic_data(item))
            db.add(topic)
            db.flush()
            extract_topic(db, topic, stock_catalog)
            imported += 1
        job.status = "success"
        job.end_time = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        failed = db.get(SyncJob, job.id)
        if failed:
            failed.status = "failed"
            failed.error_message = str(exc)[:2000]
            failed.end_time = datetime.now(timezone.utc)
            db.commit()
        raise HTTPException(500, "主题同步失败，请查看同步任务记录") from exc
    return {"job_id": job.id, "received": len(request.topics), "imported": imported, "skipped": skipped}


@app.post("/api/stocks/sync-master")
def sync_stock_master(db: Session = Depends(db_session)):
    """Refresh the A-share name dictionary used for automatic name matching."""
    try:
        rows = fetch_akshare_stock_master()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    existing = {stock.stock_code: stock for stock in db.scalars(select(Stock))}
    imported = updated = 0
    for row in rows:
        stock = existing.get(row["code"])
        if not stock:
            db.add(Stock(stock_code=row["code"], stock_name=row["name"], exchange="SH" if row["code"].startswith("6") else "SZ"))
            imported += 1
        elif stock.stock_name != row["name"]:
            stock.stock_name = row["name"]
            updated += 1
    db.commit()
    return {"provider": "akshare", "total": len(rows), "imported": imported, "updated": updated,
            "codes": [row["code"] for row in rows]}


@app.post("/api/topics/reanalyze-stocks")
def reanalyze_topic_stocks(db: Session = Depends(db_session)):
    """Add newly recognizable stock links to existing topics without replacing manual links."""
    topics = list(db.scalars(select(PlanetTopic)))
    stock_catalog = list(db.scalars(select(Stock).where(Stock.stock_name != "")))
    before = db.scalar(select(func.count()).select_from(TopicStock)) or 0
    for topic in topics:
        extract_topic(db, topic, stock_catalog)
    db.commit()
    after = db.scalar(select(func.count()).select_from(TopicStock)) or 0
    return {"topics": len(topics), "stock_master": len(stock_catalog), "links_added": after - before}


@app.post("/api/quotes/import", include_in_schema=True)
async def import_quotes(file: UploadFile = File(...), db: Session = Depends(db_session)):
    """导入行情 CSV：code,date,open,high,low,close,volume,amount,turnover_rate,adjust_type"""
    raw = await file.read()
    try:
        rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        imported = skipped = 0
        for row in rows:
            code = (row.get("code") or row.get("stock_code") or "").strip()
            if not code or not row.get("date") or not row.get("close"):
                continue
            stock = db.scalar(select(Stock).where(Stock.stock_code == code))
            if not stock:
                stock = Stock(stock_code=code, stock_name=(row.get("name") or row.get("stock_name") or ""),
                              exchange="SH" if code.startswith("6") else "SZ")
                db.add(stock); db.flush()
            elif row.get("name") or row.get("stock_name"):
                stock.stock_name = row.get("name") or row.get("stock_name")
            adjust = row.get("adjust_type") or "qfq"
            trade_date = date.fromisoformat(row["date"])
            exists = db.scalar(select(StockDailyQuote).where(StockDailyQuote.stock_id == stock.id,
                                                              StockDailyQuote.trade_date == trade_date,
                                                              StockDailyQuote.adjust_type == adjust))
            if exists:
                skipped += 1
                continue
            quote = StockDailyQuote(stock_id=stock.id, trade_date=trade_date,
                                    open=row.get("open") or row["close"], high=row.get("high") or row["close"],
                                    low=row.get("low") or row["close"], close=row["close"],
                                    volume=row.get("volume") or 0, amount=row.get("amount") or 0,
                                    turnover_rate=row.get("turnover_rate") or 0, adjust_type=adjust)
            db.add(quote); imported += 1
        db.commit()
        return {"imported": imported, "skipped": skipped}
    except (UnicodeDecodeError, ValueError, KeyError) as exc:
        db.rollback()
        raise HTTPException(400, f"invalid quote CSV: {exc}")


@app.post("/api/quotes/sync")
def sync_quotes(request: QuoteSyncIn, db: Session = Depends(db_session)):
    """Fetch quotes through optional AKShare; use /api/quotes/import for CSV."""
    if request.adjust_type not in {"qfq", "hfq", "none"}:
        raise HTTPException(400, "adjust_type must be qfq, hfq or none")
    end_date = request.end_date or date.today()
    start_date = request.start_date or end_date - timedelta(days=365)
    if start_date > end_date:
        raise HTTPException(400, "start_date must be before end_date")
    if request.stock_codes:
        codes = request.stock_codes
    else:
        rows = db.execute(select(Stock.stock_code, Stock.stock_name).join(
            TopicStock, TopicStock.stock_id == Stock.id).distinct()).all()
        codes = [code for code, name in rows if not is_st_stock(name) and not is_cdr_stock(code)]
    imported = skipped = no_data = invalid_rows = 0
    failed_codes: list[str] = []
    no_data_codes: list[str] = []
    failure_reasons: dict[str, str] = {}
    try:
        for code in codes:
            # A single code/provider response must not abort the whole batch.
            # Missing trading days are naturally omitted by AKShare; available
            # rows are still imported and the next code continues normally.
            try:
                rows = _fetch_quotes_with_retry(code, start_date, end_date,
                                                request.adjust_type, request.max_attempts)
            except Exception as exc:
                failed_codes.append(code)
                failure_reasons[code] = type(exc).__name__
                continue
            if not rows:
                no_data += 1
                no_data_codes.append(code)
                continue
            counts = _store_quote_rows(db, code, rows, request.adjust_type)
            imported += counts["imported"]
            skipped += counts["skipped"]
            invalid_rows += counts["invalid_rows"]
        db.commit()
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(503, str(exc)) from exc
    except (ValueError, KeyError) as exc:
        db.rollback()
        raise HTTPException(400, f"invalid market data: {exc}") from exc
    return {"provider": "akshare", "imported": imported, "skipped": skipped, "no_data": no_data,
            "no_data_codes": no_data_codes,
            "invalid_rows": invalid_rows, "failed": len(failed_codes), "failed_codes": failed_codes,
            "failure_reasons": failure_reasons, "max_attempts": request.max_attempts}


@app.get("/api/quotes/sync-status")
def quote_sync_status(db: Session = Depends(db_session)):
    """Report full-market checkpoint progress and actual quote coverage."""
    checkpoint = latest_quote_checkpoint()
    total_stocks = db.scalar(select(func.count()).select_from(Stock)) or 0
    database_row = db.execute(
        select(func.count(func.distinct(StockDailyQuote.stock_id)),
               func.count(StockDailyQuote.id),
               func.min(StockDailyQuote.trade_date),
               func.max(StockDailyQuote.trade_date))
        .where(StockDailyQuote.adjust_type == "qfq",
               StockDailyQuote.stock_id.in_(select(Stock.id).where(
                   ~Stock.stock_name.ilike("st%") & ~Stock.stock_name.ilike("*st%")
                   & Stock.stock_name.notlike("%退市%")
                   & Stock.stock_code.notlike("68900%"))))
    ).one()
    result = {
        "checkpoint": checkpoint is not None,
        "database": {
            "total_stocks": total_stocks,
            "stocks_with_quotes": database_row[0] or 0,
            "quote_rows": database_row[1] or 0,
            "min_date": database_row[2],
            "max_date": database_row[3],
        },
    }
    if checkpoint is None:
        return result

    completed_codes = set(checkpoint["completed_codes"])
    failed_codes = set(checkpoint.get("last_failed_codes") or []) - completed_codes
    # Count only stocks that still exist and are sync-eligible: the checkpoint
    # accumulates retired codes (purged junk, ST, CDR) that would inflate it.
    eligible_codes = {code for code, name in db.execute(
        select(Stock.stock_code, Stock.stock_name).where(Stock.stock_name != "")).all()
        if not is_st_stock(name) and not is_cdr_stock(code)}
    completed = len(completed_codes & eligible_codes)
    total = len(eligible_codes)
    failed = len(failed_codes & eligible_codes)
    processed = min(completed + failed, total)
    updated_at = checkpoint["updated_at"]
    stored_status = checkpoint.get("status")
    fresh = datetime.now(timezone.utc) - updated_at <= timedelta(minutes=3)
    if stored_status == "analyzing" and fresh:
        status, status_text = "analyzing", "行情已同步，正在自动重算分析结果"
    elif stored_status == "analyzing":
        status, status_text = "paused", "自动重算已中断，可点击后台继续/重试"
    elif stored_status == "failed":
        status, status_text = "failed", "后台任务失败，可点击重新同步"
    elif total and processed >= total and failed:
        status = "completed_with_failures"
        status_text = (f"已完成并重算（{failed} 只重试后仍失败）"
                       if checkpoint.get("analysis_status") == "completed"
                       else f"行情已完成（{failed} 只失败），点击后台继续/重试后自动重算")
    elif total and completed >= total:
        status, status_text = "completed", ("已完成并重算" if checkpoint.get("analysis_status") == "completed"
                                              else "行情已完成，分析尚未自动重算")
    elif stored_status == "paused":
        status, status_text = "paused", "已暂停，可从断点继续"
    elif stored_status == "running" and fresh:
        status, status_text = "running", "后台同步中"
    elif stored_status == "running":
        status, status_text = "paused", "后台同步已中断，可从断点继续"
    elif fresh:
        status, status_text = "running", "同步中"
    else:
        status, status_text = "paused", "已暂停，可从断点继续"
    result["progress"] = {
        "status": status,
        "status_text": status_text,
        "completed": completed,
        "total": total,
        "remaining": max(total - processed, 0),
        "percent": round(processed * 100 / total, 2) if total else 0,
        "failed": failed,
        "failed_codes": sorted(failed_codes)[:50],
        "failure_reasons": checkpoint.get("failure_reasons") or {},
        "start_date": checkpoint.get("start_date"),
        "end_date": checkpoint.get("end_date"),
        "updated_at": updated_at,
        "state_file": checkpoint["state_file"],
        "analysis_status": checkpoint.get("analysis_status"),
        "analysis_event_returns": checkpoint.get("analysis_event_returns"),
        "analysis_completed_at": checkpoint.get("analysis_completed_at"),
    }
    return result


@app.post("/api/quotes/sync-resume")
def quote_sync_resume(db: Session = Depends(db_session)):
    """Continue the newest full-market quote sync from its checkpoint."""
    checkpoint = latest_quote_checkpoint()
    if not checkpoint:
        raise HTTPException(404, "没有找到全量行情断点文件，无法续跑")
    fresh = datetime.now(timezone.utc) - checkpoint["updated_at"] <= timedelta(minutes=3)
    if ((checkpoint.get("status") in {"running", "analyzing"} and fresh)
            or (not checkpoint.get("status") and fresh)):
        raise HTTPException(409, "同步正在运行中，请稍后再试")
    if not _quote_sync_lock.acquire(blocking=False):
        raise HTTPException(409, "同步正在运行中，请稍后再试")
    # Always extend the window to today so a missed scheduled run can be
    # caught up with one click; already-synced days are skipped by the
    # checkpoint and the per-row existence check.
    checkpoint_start = checkpoint.get("start_date") or f"{date.today().year}-01-01"
    checkpoint_end = checkpoint.get("end_date") or date.today().isoformat()
    try:
        end_date = max(date.fromisoformat(checkpoint_end), date.today()).isoformat()
    except ValueError:
        end_date = date.today().isoformat()
    threading.Thread(target=_resume_quote_sync_worker,
                     args=(checkpoint["state_file"], checkpoint_start, end_date),
                     daemon=True).start()
    return {"status": "started", "state_file": checkpoint["state_file"],
            "start_date": checkpoint_start, "end_date": end_date,
            "completed": len(set(checkpoint["completed_codes"])),
            "total": checkpoint.get("total") or 0,
            "retrying": len(set(checkpoint.get("last_failed_codes") or [])
                            - set(checkpoint["completed_codes"]))}


def purge_excluded_stocks(db: Session) -> dict:
    """Remove data for stocks excluded from syncing (ST/delisted/CDR): their
    quotes and event returns are deleted, but the stock rows and topic links
    stay — topic_stock has a NOT-NULL foreign key, and mentions should remain
    traceable. The status page excludes them from its counts."""
    excluded = list(db.scalars(select(Stock).where(
        (Stock.stock_name != "") & (
            Stock.stock_name.ilike("st%") | Stock.stock_name.ilike("*st%")
            | Stock.stock_name.like("%退市%") | Stock.stock_code.like("68900%")))))
    ids = [stock.id for stock in excluded]
    if not ids:
        return {"stocks_removed": 0, "quote_rows_removed": 0,
                "event_returns_removed": 0, "topic_links_kept": 0}
    quotes_removed = db.execute(delete(StockDailyQuote).where(StockDailyQuote.stock_id.in_(ids)))
    returns_removed = db.execute(delete(StockEventReturn).where(StockEventReturn.stock_id.in_(ids)))
    db.commit()
    return {"stocks_removed": 0, "quote_rows_removed": getattr(quotes_removed, "rowcount", 0) or 0,
            "event_returns_removed": getattr(returns_removed, "rowcount", 0) or 0,
            "topic_links_kept": db.scalar(select(func.count()).select_from(TopicStock).where(
                TopicStock.stock_id.in_(ids))) or 0}


def purge_unnamed_stocks(db: Session) -> dict:
    """Delete stocks with no name: they come from false-positive code matches in
    topics (e.g. Korean codes like 000660) and can never have A-share quotes.
    Removes their quotes and topic links so the gap report stops flagging them."""
    unnamed = list(db.scalars(select(Stock).where(Stock.stock_name == "")))
    ids = [stock.id for stock in unnamed]
    if not ids:
        return {"stocks_removed": 0, "quote_rows_removed": 0, "topic_links_removed": 0}
    quotes_removed = db.execute(delete(StockDailyQuote).where(StockDailyQuote.stock_id.in_(ids)))
    links_removed = db.execute(delete(TopicStock).where(TopicStock.stock_id.in_(ids)))
    db.execute(delete(StockEventReturn).where(StockEventReturn.stock_id.in_(ids)))
    for stock in unnamed:
        db.delete(stock)
    db.commit()
    return {"stocks_removed": len(ids), "quote_rows_removed": getattr(quotes_removed, "rowcount", 0) or 0,
            "topic_links_removed": getattr(links_removed, "rowcount", 0) or 0}


GAP_STATE_FILE = Path("/data") / "quote-gap-sync-state.json"


def _trading_days(db: Session, start: date, end: date) -> list[date]:
    """A-share trading days in [start, end] via AKShare's calendar, cached for
    one hour per (start, end) pair to avoid hammering the source."""
    cache_key = (start.isoformat(), end.isoformat())
    cached = _trading_days_cache.get(cache_key)
    now = _time_module.monotonic()
    if cached and now - cached[0] < 3600:
        return cached[1]
    try:
        import akshare as ak
        rows = ak.tool_trade_date_hist_sina().to_dict("records")
    except Exception as exc:
        raise HTTPException(503, f"交易日历获取失败：{type(exc).__name__}") from exc
    days = _trading_days_from_rows(rows, start, end)
    _trading_days_cache.clear()
    _trading_days_cache[cache_key] = (now, days)
    return days


_trading_days_cache: dict[tuple[str, str], tuple[float, list[date]]] = {}


@app.post("/api/stocks/purge-excluded")
def purge_excluded_stocks_endpoint(db: Session = Depends(db_session)):
    """Delete ST/delisted/CDR stocks with their quotes; topic links are kept."""
    return purge_excluded_stocks(db)


@app.post("/api/stocks/purge-unnamed")
def purge_unnamed_stocks_endpoint(db: Session = Depends(db_session)):
    """Remove false-positive stocks (unnamed codes matched in topics, e.g. foreign tickers)."""
    return purge_unnamed_stocks(db)


_gap_detect_lock = threading.Lock()
_gap_detect_state: dict = {"status": "idle"}  # idle|running|done, progress, result


@app.get("/api/quotes/gap")
def quote_gap_report():
    """Latest gap-detection result (from the background detection task)."""
    state = _gap_detect_state
    if state.get("status") != "done":
        return {"status": state.get("status", "idle"),
                "progress": state.get("progress", {"done": 0, "total": 0})}
    return {"status": "done", **state["result"]}


@app.post("/api/quotes/gap/detect")
def quote_gap_detect(db: Session = Depends(db_session)):
    """Start background gap detection (trading calendar + DB scan + per-stock
    suspension probes) and return immediately; poll /api/quotes/gap for progress."""
    if not _gap_detect_lock.acquire(blocking=False):
        return {"status": "running"}
    threading.Thread(target=_gap_detect_thread, daemon=True).start()
    return {"status": "started"}


def _gap_detect_thread():
    db = SessionLocal()
    try:
        _gap_detect_state.update(status="running", progress={"done": 0, "total": 0})
        year_start = date(date.today().year, 1, 1)
        trading_days = _trading_days(db, year_start, date.today())
        stock_rows = db.execute(select(Stock.id, Stock.stock_code, Stock.stock_name)
                                .where(Stock.stock_code != "")).all()
        stocks = {row[0]: (row[1], row[2]) for row in stock_rows}
        quote_rows = db.execute(
            select(StockDailyQuote.stock_id, StockDailyQuote.trade_date)
            .where(StockDailyQuote.adjust_type == "qfq",
                   StockDailyQuote.trade_date >= year_start)).all()

        def on_progress(done: int, total: int):
            _gap_detect_state["progress"] = {"done": done, "total": total}

        suspended = _probe_suspensions(
            [{"stock_id": row[0], "trade_date": row[1]} for row in quote_rows],
            trading_days, stocks, progress=on_progress)
        gaps = detect_quote_gaps(
            [{"stock_id": row[0], "trade_date": row[1]} for row in quote_rows],
            trading_days, stocks,
            source_probe=lambda code, day: day not in suspended.get(code, set()))
        _gap_detect_state["result"] = {
            "trading_days": [day.isoformat() for day in trading_days],
            "gaps": {day.isoformat(): entries for day, entries in gaps.items()},
            "total_missing": sum(len(entries) for entries in gaps.values()),
        }
        _gap_detect_state["status"] = "done"
    except Exception:
        log.exception("gap detect crashed")
        _gap_detect_state["status"] = "error"
    finally:
        db.close()
        _gap_detect_lock.release()


def _probe_suspensions(quote_rows: list[dict], trading_days: list[date],
                       stocks: dict[int, tuple[str, str]],
                       progress=None, max_attempts: int = 1) -> dict[str, set[date]]:
    """For each stock with mid-gaps, ask the data source once over its gap span
    and return {code: {days the provider also lacks}} — those are suspensions.
    Stocks without mid-gaps are not probed. `progress(done, total)` fires per stock."""
    dates_by_code: dict[str, set[date]] = {}
    for row in quote_rows:
        code = stocks.get(row["stock_id"], ("", ""))[0]
        if code:
            dates_by_code.setdefault(code, set()).add(row["trade_date"])
    candidates: list[tuple[str, date, date, list[date]]] = []
    for code, dates in dates_by_code.items():
        if not dates:
            continue
        first, last = min(dates), max(dates)
        mid_gaps = [day for day in trading_days if first <= day <= last and day not in dates]
        if mid_gaps:
            candidates.append((code, min(mid_gaps), last, mid_gaps))
    suspended: dict[str, set[date]] = {}
    total = len(candidates)
    for index, (code, start, end, mid_gaps) in enumerate(sorted(candidates), start=1):
        try:
            rows = _fetch_quotes_with_retry(code, start, end, "qfq", max_attempts)
            have = {date.fromisoformat(row["date"]) for row in rows if row.get("date")}
        except Exception:
            have = None  # probe failed: don't classify anything for this stock
        if have is not None:
            missing = {day for day in mid_gaps if day not in have}
            if missing:
                suspended[code] = missing
        if progress is not None:
            progress(index, total)
    return suspended


_gap_sync_lock = threading.Lock()


@app.post("/api/quotes/gap-sync")
def quote_gap_sync(start_date: str = "", end_date: str = "", db: Session = Depends(db_session)):
    """Backfill quotes for the stocks missing them; scope to [start_date, end_date]
    or default to all detected gap days."""
    year_start = date(date.today().year, 1, 1)
    today = date.today()
    if start_date or end_date:
        try:
            start = date.fromisoformat(start_date) if start_date else year_start
            end = date.fromisoformat(end_date) if end_date else today
        except ValueError as exc:
            raise HTTPException(400, "日期格式应为 YYYY-MM-DD") from exc
        _validate_gap_range(start, end)
    else:
        # No explicit range: backfill everything already detectable as missing;
        # an unclosed today is silently excluded instead of rejecting the click.
        start, end = _default_gap_range()
    trading_days = [day for day in _trading_days(db, year_start, today) if start <= day <= end]
    stock_rows = db.execute(select(Stock.id, Stock.stock_code, Stock.stock_name)
                            .where(Stock.stock_code != "")).all()
    stocks = {row[0]: (row[1], row[2]) for row in stock_rows}
    quote_rows = db.execute(
        select(StockDailyQuote.stock_id, StockDailyQuote.trade_date)
        .where(StockDailyQuote.adjust_type == "qfq",
               StockDailyQuote.trade_date >= start,
               StockDailyQuote.trade_date <= end)).all()
    gaps = detect_quote_gaps([{"stock_id": row[0], "trade_date": row[1]} for row in quote_rows],
                             trading_days, stocks)
    if not gaps:
        return {"status": "no_gaps", "message": "所选范围内没有检测到缺失行情"}
    state = {"status": "pending", "targets": {day.isoformat(): entries for day, entries in gaps.items()},
             "failures": [], "start_date": start.isoformat(), "end_date": end.isoformat()}
    if not _gap_sync_lock.acquire(blocking=False):
        raise HTTPException(409, "补漏同步正在运行中，请稍后再试")
    try:
        _save_quote_checkpoint(GAP_STATE_FILE, state)
    except OSError:
        _gap_sync_lock.release()
        raise HTTPException(500, "补漏进度文件写入失败，请重试")
    threading.Thread(target=_gap_sync_thread, daemon=True).start()
    return {"status": "started", "days": len(gaps),
            "stocks": len({e["code"] for entries in gaps.values() for e in entries})}


def _gap_sync_thread():
    db = SessionLocal()
    state: dict = {}
    try:
        state = json.loads(GAP_STATE_FILE.read_text(encoding="utf-8")) if GAP_STATE_FILE.exists() else {}
        if state.get("status") != "pending":
            log.warning("gap sync state file missing pending state, aborting")
            return
        _quote_gap_sync_worker(db, state, progress_path=GAP_STATE_FILE)
    except Exception:
        state["status"] = state.get("status") or "failed"
        state["error"] = type(state.get("error") or Exception()).__name__
        try:
            _save_quote_checkpoint(GAP_STATE_FILE, state)
        except OSError:
            pass
        log.exception("quote gap sync worker crashed")
    finally:
        db.close()
        _gap_sync_lock.release()


@app.get("/api/quotes/gap-sync-status")
def quote_gap_sync_status():
    """Progress and per-day failures of the last gap-sync run."""
    if not GAP_STATE_FILE.exists():
        return {"status": "idle"}
    try:
        state = json.loads(GAP_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "idle"}
    return {"status": state.get("status"), "completed": state.get("completed", 0),
            "total": state.get("total", 0), "failures": state.get("failures") or [],
            "suspended": state.get("suspended") or [],
            "start_date": state.get("start_date"), "end_date": state.get("end_date"),
            "finished_at": state.get("finished_at")}


@app.get("/api/stocks/search")
def search_stocks(keyword: str = "", market: str = "all", limit: int = 20,
                  db: Session = Depends(db_session)):
    if market not in MARKETS:
        raise HTTPException(400, "market must be all, main, gem, star or bse")
    if limit < 1 or limit > 50:
        raise HTTPException(400, "limit must be between 1 and 50")
    statement = select(Stock)
    clause = market_clause(Stock.stock_code, market)
    if clause is not None:
        statement = statement.where(clause)
    needle = keyword.strip()
    if needle:
        pattern = f"%{needle}%"
        statement = statement.where(or_(Stock.stock_code.ilike(pattern), Stock.stock_name.ilike(pattern)))
    rows = db.scalars(statement.order_by(Stock.stock_code).limit(limit)).all()
    return [{"code": stock.stock_code, "name": stock.stock_name,
             "market": market_for_code(stock.stock_code)} for stock in rows]


@app.get("/api/stocks/market-list")
def stock_market_list(keyword: str = "", market: str = "all", period: str = "1d",
                      page: int = 1, page_size: int = 100, sort: str = "desc",
                      adjust_type: str = "qfq", db: Session = Depends(db_session)):
    if market not in MARKETS:
        raise HTTPException(400, "market must be all, main, gem, star or bse")
    if period not in PERIODS:
        raise HTTPException(400, "unsupported period")
    if page < 1 or page_size not in {100, 200, 500}:
        raise HTTPException(400, "page must be >= 1 and page_size must be 100, 200 or 500")
    if sort not in {"asc", "desc"}:
        raise HTTPException(400, "sort must be asc or desc")
    if adjust_type not in {"qfq", "hfq", "none"}:
        raise HTTPException(400, "adjust_type must be qfq, hfq or none")

    statement = select(Stock)
    clause = market_clause(Stock.stock_code, market)
    if clause is not None:
        statement = statement.where(clause)
    needle = keyword.strip()
    if needle:
        pattern = f"%{needle}%"
        statement = statement.where(or_(Stock.stock_code.ilike(pattern), Stock.stock_name.ilike(pattern)))
    stocks = db.scalars(statement.order_by(Stock.stock_code)).all()
    total = len(stocks)
    if not stocks:
        return {"items": [], "total": 0, "page": page, "page_size": page_size,
                "period": period, "sort": sort}

    stock_ids = [stock.id for stock in stocks]
    ranked = select(
        StockDailyQuote.stock_id, StockDailyQuote.trade_date, StockDailyQuote.open,
        StockDailyQuote.high, StockDailyQuote.low, StockDailyQuote.close,
        StockDailyQuote.volume, StockDailyQuote.turnover_rate,
        func.row_number().over(
            partition_by=StockDailyQuote.stock_id,
            order_by=StockDailyQuote.trade_date.desc(),
        ).label("rn"),
    ).where(StockDailyQuote.stock_id.in_(stock_ids),
            StockDailyQuote.adjust_type == adjust_type).subquery()
    base_rank = (int(period[:-1]) + 1) if period[:-1].isdigit() else 1
    ranked_rows = db.execute(select(ranked).where(ranked.c.rn.in_({1, base_rank}))).all()
    quote_by_stock: dict[int, dict[int, object]] = {}
    for row in ranked_rows:
        quote_by_stock.setdefault(row.stock_id, {})[row.rn] = row

    baseline_by_stock: dict[int, object] = {}
    if period in {"mtd", "ytd"}:
        latest_date = db.scalar(select(func.max(StockDailyQuote.trade_date)).where(
            StockDailyQuote.stock_id.in_(stock_ids), StockDailyQuote.adjust_type == adjust_type))
        if latest_date:
            start_date = latest_date.replace(day=1) if period == "mtd" else latest_date.replace(month=1, day=1)
            baseline = select(StockDailyQuote.stock_id,
                              func.min(StockDailyQuote.trade_date).label("base_date")).where(
                StockDailyQuote.stock_id.in_(stock_ids), StockDailyQuote.adjust_type == adjust_type,
                StockDailyQuote.trade_date >= start_date,
                StockDailyQuote.trade_date <= latest_date,
            ).group_by(StockDailyQuote.stock_id).subquery()
            baseline_rows = db.execute(
                select(StockDailyQuote.stock_id, StockDailyQuote.close)
                .join(baseline, (baseline.c.stock_id == StockDailyQuote.stock_id)
                      & (baseline.c.base_date == StockDailyQuote.trade_date))
                .where(StockDailyQuote.adjust_type == adjust_type)
            ).all()
            baseline_by_stock = {row.stock_id: row.close for row in baseline_rows}

    items = []
    for stock in stocks:
        quotes = quote_by_stock.get(stock.id, {})
        latest = quotes.get(1)
        if not latest:
            change = None
            current = volume = turnover = latest_date = None
        else:
            base = baseline_by_stock.get(stock.id) if period in {"mtd", "ytd"} else quotes.get(base_rank)
            base_close = base.close if hasattr(base, "close") else base
            change = float((latest.close / base_close - 1) * 100) if base_close else None
            current, volume, turnover, latest_date = (float(latest.close), float(latest.volume or 0),
                                                       float(latest.turnover_rate or 0), latest.trade_date)
        items.append({"code": stock.stock_code, "name": stock.stock_name,
                      "market": market_for_code(stock.stock_code),
                      "latest_date": latest_date, "current_price": current,
                      "change_pct": change, "volume": volume, "turnover_rate": turnover})
    if sort == "desc":
        items.sort(key=lambda item: (item["change_pct"] is None,
                                     -(item["change_pct"] or 0)))
    else:
        items.sort(key=lambda item: (item["change_pct"] is None,
                                     item["change_pct"] if item["change_pct"] is not None else 0))
    offset = (page - 1) * page_size
    return {"items": items[offset:offset + page_size], "total": total,
            "page": page, "page_size": page_size, "period": period, "sort": sort}


@app.get("/api/strategies/zzr/screen")
def zzr_screen(signal_date: date | None = None, market: str = "all", page: int = 1,
               page_size: int = 100, adjust_type: str = "qfq",
               db: Session = Depends(db_session)):
    """Scan close-confirmed 紫紫红 signals for one trading date."""
    if market not in MARKETS:
        raise HTTPException(400, "market must be all, main, gem, star or bse")
    if page < 1 or page_size < 1 or page_size > 500:
        raise HTTPException(400, "page must be >= 1 and page_size must be between 1 and 500")
    if adjust_type not in {"qfq", "hfq", "none"}:
        raise HTTPException(400, "adjust_type must be qfq, hfq or none")

    stock_statement = select(Stock)
    clause = market_clause(Stock.stock_code, market)
    if clause is not None:
        stock_statement = stock_statement.where(clause)
    stocks = list(db.scalars(stock_statement.order_by(Stock.stock_code)))
    stock_by_id = {stock.id: stock for stock in stocks}
    if not stocks:
        return {"items": [], "total": 0, "page": page, "page_size": page_size,
                "signal_date": signal_date, "scanned": 0}

    stock_ids = list(stock_by_id)
    target_date = signal_date or db.scalar(select(func.max(StockDailyQuote.trade_date)).where(
        StockDailyQuote.stock_id.in_(stock_ids), StockDailyQuote.adjust_type == adjust_type))
    if not target_date:
        return {"items": [], "total": 0, "page": page, "page_size": page_size,
                "signal_date": None, "scanned": len(stocks)}

    statement = (select(StockDailyQuote.stock_id, StockDailyQuote.trade_date,
                        StockDailyQuote.open, StockDailyQuote.high, StockDailyQuote.low,
                        StockDailyQuote.close, StockDailyQuote.volume)
                 .where(StockDailyQuote.stock_id.in_(stock_ids),
                        StockDailyQuote.adjust_type == adjust_type,
                        StockDailyQuote.trade_date <= target_date)
                 .order_by(StockDailyQuote.stock_id, StockDailyQuote.trade_date))
    matches: list[dict] = []
    current_id: int | None = None
    rows: list[dict] = []

    def collect(stock_id: int | None, quotes: list[dict]) -> None:
        if stock_id is None or not quotes or quotes[-1]["date"] != target_date:
            return
        point = calculate_zzr(quotes)[-1]
        if not point["signal"]:
            return
        stock = stock_by_id[stock_id]
        matches.append({"code": stock.stock_code, "name": stock.stock_name,
                        "market": market_for_code(stock.stock_code),
                        "signal_date": target_date, "close": quotes[-1]["close"], **point})

    for row in db.execute(statement).yield_per(2000):
        if current_id is not None and row.stock_id != current_id:
            collect(current_id, rows)
            rows = []
        current_id = row.stock_id
        rows.append({"date": row.trade_date, "open": float(row.open), "high": float(row.high),
                     "low": float(row.low), "close": float(row.close),
                     "volume": float(row.volume or 0)})
    collect(current_id, rows)
    matches.sort(key=lambda item: (item["cmf"], item["mom"]), reverse=True)
    offset = (page - 1) * page_size
    return {"items": matches[offset:offset + page_size], "total": len(matches),
            "page": page, "page_size": page_size, "signal_date": target_date,
            "scanned": len(stocks)}


@app.get("/api/quotes/history/{stock_code}")
def quote_history(stock_code: str, limit: int = 500, adjust_type: str = "qfq",
                  db: Session = Depends(db_session)):
    """Return daily OHLCV rows for the standalone candlestick page."""
    if adjust_type not in {"qfq", "hfq", "none"}:
        raise HTTPException(400, "adjust_type must be qfq, hfq or none")
    if limit < 1 or limit > 2000:
        raise HTTPException(400, "limit must be between 1 and 2000")
    code = stock_code.strip().upper().replace(".", "")
    if not code:
        raise HTTPException(400, "stock_code is required")
    stock = db.scalar(select(Stock).where(Stock.stock_code == code))
    if not stock:
        raise HTTPException(404, "未找到该股票或尚无行情数据")

    rows = list(db.scalars(
        select(StockDailyQuote)
        .where(StockDailyQuote.stock_id == stock.id,
               StockDailyQuote.adjust_type == adjust_type)
        .order_by(StockDailyQuote.trade_date.desc())
        .limit(limit + 1)
    ))
    rows.reverse()
    if not rows:
        raise HTTPException(404, "该股票暂无行情数据")

    has_previous = len(rows) > limit
    history = rows[-limit:] if has_previous else rows
    strategy_points = calculate_zzr([{
        "open": float(row.open), "high": float(row.high), "low": float(row.low),
        "close": float(row.close), "volume": float(row.volume or 0),
    } for row in rows])
    strategy_by_date = {row.trade_date: point for row, point in zip(rows, strategy_points)}
    previous_close = float(rows[0].close) if has_previous else None
    items = []
    for row in history:
        close = float(row.close)
        change_pct = ((close / previous_close) - 1) * 100 if previous_close else None
        items.append({
            "date": row.trade_date.isoformat(),
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": close,
            "volume": float(row.volume or 0),
            "change_pct": change_pct,
            "zzr": strategy_by_date[row.trade_date],
        })
        previous_close = close
    latest = items[-1]
    return {
        "code": stock.stock_code,
        "name": stock.stock_name,
        "adjust_type": adjust_type,
        "latest_date": latest["date"],
        "current_price": latest["close"],
        "change_pct": latest["change_pct"],
        "zzr_signal_count": sum(1 for item in items if item["zzr"]["signal"]),
        "items": items,
    }


@app.get("/api/stocks/mentioned")
def mentioned_stock_codes(db: Session = Depends(db_session)):
    """Codes referenced by at least one Knowledge Planet topic."""
    return list(db.scalars(select(Stock.stock_code).join(TopicStock).distinct().order_by(Stock.stock_code)))


@app.post("/api/analyze/rebuild")
def analyze(db: Session = Depends(db_session)):
    try:
        rebuild_returns(db)
        event_returns = db.scalar(select(func.count()).select_from(StockEventReturn)) or 0
        db.commit()
        return {"status": "ok", "event_returns": event_returns}
    except Exception as exc:
        db.rollback()
        log.exception("收益重算失败")
        raise HTTPException(500, "收益重算失败，请查看应用日志") from exc


@app.post("/api/analyze/discover-keywords")
def discover_keywords_api(db: Session = Depends(db_session)):
    """Rebuild outcomes, then extract recurring non-seed terms and persist links."""
    try:
        rebuild_returns(db)
        result = discover_auto_keywords(db)
        result["event_returns"] = db.scalar(select(func.count()).select_from(StockEventReturn)) or 0
        db.commit()
        return result
    except Exception as exc:
        db.rollback()
        log.exception("荐股强调词扫描失败")
        raise HTTPException(500, "自动关键词发现失败，请查看应用日志") from exc


@app.get("/api/keywords/manual")
def list_manual_keywords(include_inactive: bool = True, db: Session = Depends(db_session)):
    ensure_manual_keywords(db)
    statement = select(Keyword).where(Keyword.category != AUTO_KEYWORD_CATEGORY)
    if not include_inactive:
        statement = statement.where(Keyword.active.is_(True))
    keywords = list(db.scalars(statement.order_by(Keyword.active.desc(), Keyword.keyword)))
    counts = dict(db.execute(
        select(TopicKeyword.keyword_id, func.count(func.distinct(TopicKeyword.topic_id)))
        .group_by(TopicKeyword.keyword_id)
    ).all())
    return [manual_keyword_item(keyword, counts) for keyword in keywords]


@app.post("/api/keywords/manual")
def create_manual_keyword(request: ManualKeywordIn, db: Session = Depends(db_session)):
    term = normalize_manual_keyword(request.keyword)
    keyword = db.scalar(select(Keyword).where(Keyword.normalized_keyword == term))
    if keyword:
        if keyword.category == AUTO_KEYWORD_CATEGORY:
            raise HTTPException(409, "该词已存在于自动强调词库，不能重复创建")
        keyword.keyword = term
        keyword.category = MANUAL_KEYWORD_CATEGORY
        keyword.active = True
        db.commit()
        return manual_keyword_item(keyword, {})
    keyword = Keyword(keyword=term, normalized_keyword=term,
                      category=MANUAL_KEYWORD_CATEGORY, active=True)
    db.add(keyword)
    db.commit()
    db.refresh(keyword)
    return manual_keyword_item(keyword, {})


@app.put("/api/keywords/manual/{keyword_id}")
def update_manual_keyword(keyword_id: int, request: ManualKeywordIn, db: Session = Depends(db_session)):
    keyword = db.get(Keyword, keyword_id)
    if not keyword or keyword.category == AUTO_KEYWORD_CATEGORY:
        raise HTTPException(404, "manual keyword not found")
    term = normalize_manual_keyword(request.keyword)
    conflict = db.scalar(select(Keyword).where(
        Keyword.normalized_keyword == term, Keyword.id != keyword_id))
    if conflict:
        raise HTTPException(409, "该关键词已经存在")
    keyword.keyword = term
    keyword.normalized_keyword = term
    keyword.category = MANUAL_KEYWORD_CATEGORY
    db.commit()
    return manual_keyword_item(keyword, {})


@app.patch("/api/keywords/manual/{keyword_id}")
def set_manual_keyword_active(keyword_id: int, request: ManualKeywordActiveIn,
                              db: Session = Depends(db_session)):
    keyword = db.get(Keyword, keyword_id)
    if not keyword or keyword.category == AUTO_KEYWORD_CATEGORY:
        raise HTTPException(404, "manual keyword not found")
    keyword.active = request.active
    keyword.category = MANUAL_KEYWORD_CATEGORY
    db.commit()
    return manual_keyword_item(keyword, {})


@app.get("/api/stats/keywords", response_model=list[KeywordStat])
def stats(keyword: str | None = None, stock_code: str | None = None,
          start_date: date | None = None, end_date: date | None = None,
          db: Session = Depends(db_session)):
    return keyword_stats(db, keyword_filter=keyword, stock_code=stock_code,
                         start_date=start_date, end_date=end_date)


@app.get("/api/stats/auto-keywords")
def auto_keyword_stats(keyword: str | None = None, stock_code: str | None = None,
                       min_samples: int = AUTO_KEYWORD_MIN_SAMPLES,
                       db: Session = Depends(db_session)):
    if min_samples < 1 or min_samples > 1000:
        raise HTTPException(400, "min_samples must be between 1 and 1000")
    return discovered_keyword_stats(db, keyword_filter=keyword, stock_code=stock_code,
                                    min_samples=min_samples)


@app.get("/api/stats/auto-keywords/{keyword_id}")
def auto_keyword_detail(keyword_id: int, limit: int = 100, db: Session = Depends(db_session)):
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be between 1 and 200")
    result = discovered_keyword_detail(db, keyword_id, limit=limit)
    if not result:
        raise HTTPException(404, "auto keyword not found")
    return result


@app.get("/api/export/keywords.csv", include_in_schema=True)
def export_keywords(keyword: str | None = None, stock_code: str | None = None,
                    start_date: date | None = None, end_date: date | None = None,
                    db: Session = Depends(db_session)):
    rows = keyword_stats(db, keyword_filter=keyword, stock_code=stock_code,
                         start_date=start_date, end_date=end_date)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=("keyword", "topic_count", "stock_count", "avg_return_5d",
                                                "rise_rate_10pct", "avg_return_20d", "rise_rate_10pct_20d",
                                                "eligible_count_20d", "sample_sufficient"))
    writer.writeheader()
    writer.writerows(rows)
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=keyword_stats.csv"})


@app.get("/api/topics")
def topics(limit: int = 50, db: Session = Depends(db_session)):
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be between 1 and 200")
    return [{key: value for key, value in topic_summary(t).items() if key != "content"}
            for t in db.scalars(select(PlanetTopic).order_by(PlanetTopic.published_at.desc()).limit(limit))]


@app.get("/api/topics/search")
def search_topics(keywords: str = "", page: int = 1, page_size: int = 20, db: Session = Depends(db_session)):
    if page < 1 or page_size < 1 or page_size > 100:
        raise HTTPException(400, "page must be >= 1 and page_size must be between 1 and 100")
    terms = split_keywords(keywords)
    statement = select(PlanetTopic)
    for term in terms:
        pattern = f"%{term}%"
        statement = statement.where(or_(PlanetTopic.title.ilike(pattern), PlanetTopic.content.ilike(pattern)))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.scalars(statement.order_by(PlanetTopic.published_at.desc(), PlanetTopic.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [topic_summary(topic) for topic in rows], "total": total, "page": page,
            "page_size": page_size, "keywords": terms}


@app.get("/api/topics/{topic_id}")
def topic_detail(topic_id: str, db: Session = Depends(db_session)):
    topic = db.scalar(select(PlanetTopic).where(PlanetTopic.topic_id == topic_id))
    if not topic:
        raise HTTPException(404, "topic not found")
    stocks = db.execute(select(TopicStock, Stock).join(Stock, Stock.id == TopicStock.stock_id)
                        .where(TopicStock.topic_id == topic.id)).all()
    keywords = db.execute(select(TopicKeyword, Keyword).join(Keyword, Keyword.id == TopicKeyword.keyword_id)
                          .where(TopicKeyword.topic_id == topic.id)).all()
    returns = db.execute(select(StockEventReturn).where(StockEventReturn.topic_id == topic.id)).scalars().all()
    return {"topic_id": topic.topic_id, "title": topic.title, "content": topic.content, "author": topic.author,
            "published_at": topic.published_at, "source_url": topic.source_url,
            "tags": json.loads(topic.tags or "[]"),
            "stocks": [{"code": stock.stock_code, "name": stock.stock_name, "context": link.mention_context,
                        "confidence": float(link.confidence)} for link, stock in stocks],
            "keywords": [{"keyword": keyword.keyword, "context": link.context} for link, keyword in keywords],
            "returns": [{"stock_code": db.get(Stock, row.stock_id).stock_code, "event_date": row.event_date,
                         "return_1d": row.return_1d, "return_3d": row.return_3d, "return_5d": row.return_5d,
                         "return_10d": row.return_10d, "return_20d": row.return_20d,
                         "max_return_5d": row.max_return_5d, "max_return_20d": row.max_return_20d,
                         "rise_10pct_flag": row.rise_10pct_flag,
                         "rise_10pct_20d_flag": row.rise_10pct_20d_flag} for row in returns]}


@app.put("/api/topics/{topic_id}/annotations")
def update_annotations(topic_id: str, request: TopicAnnotationsIn, db: Session = Depends(db_session)):
    """Replace extracted links after a human review."""
    topic = db.scalar(select(PlanetTopic).where(PlanetTopic.topic_id == topic_id))
    if not topic:
        raise HTTPException(404, "topic not found")
    db.execute(delete(TopicStock).where(TopicStock.topic_id == topic.id))
    db.execute(delete(TopicKeyword).where(TopicKeyword.topic_id == topic.id))
    db.execute(delete(StockEventReturn).where(StockEventReturn.topic_id == topic.id))
    for item in request.stocks:
        stock = db.scalar(select(Stock).where(Stock.stock_code == item.code))
        if not stock:
            stock = Stock(stock_code=item.code, stock_name=item.name,
                          exchange="SH" if item.code.startswith("6") else "SZ")
            db.add(stock)
            db.flush()
        elif item.name:
            stock.stock_name = item.name
        db.add(TopicStock(topic_id=topic.id, stock_id=stock.id, mention_context=item.context,
                          confidence=item.confidence))
    keywords = list(dict.fromkeys(item.strip() for item in request.keywords if item.strip()))
    for raw in keywords:
        keyword = db.scalar(select(Keyword).where(Keyword.normalized_keyword == raw))
        if not keyword:
            keyword = Keyword(keyword=raw, normalized_keyword=raw,
                              category=MANUAL_KEYWORD_CATEGORY, active=True)
            db.add(keyword)
            db.flush()
        elif keyword.category != AUTO_KEYWORD_CATEGORY:
            keyword.category = MANUAL_KEYWORD_CATEGORY
            keyword.active = True
        db.add(TopicKeyword(topic_id=topic.id, keyword_id=keyword.id, context="人工标注"))
    db.commit()
    return {"topic_id": topic_id, "stocks": len(request.stocks), "keywords": len(keywords)}


@app.get("/api/sync/jobs")
def sync_jobs(limit: int = 20, db: Session = Depends(db_session)):
    if limit < 1 or limit > 100:
        raise HTTPException(400, "limit must be between 1 and 100")
    return [{"id": job.id, "start_time": job.start_time, "end_time": job.end_time,
             "status": job.status, "error_message": job.error_message}
            for job in db.scalars(select(SyncJob).order_by(SyncJob.id.desc()).limit(limit))]
