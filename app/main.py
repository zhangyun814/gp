import csv
import io
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session
from .analyzer import extract_topic, keyword_stats, rebuild_returns
from .db import SessionLocal, init_db
from .market import fetch_akshare_quotes, fetch_akshare_stock_master
from .models import (Keyword, PlanetCircle, PlanetTopic, Stock, StockDailyQuote,
                     StockEventReturn, SyncJob, TopicKeyword, TopicStock)
from .schemas import (KeywordStat, QuoteSyncIn, TopicAnnotationsIn, TopicImportResult, TopicIn,
                      ZsxqSyncIn, ZsxqSyncResult)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("planet-stock")


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="知识星球观点分析", version="0.1.0", lifespan=lifespan)

KEYWORD_SPLIT_RE = re.compile(r"[\s,，]+")


def split_keywords(value: str) -> list[str]:
    return list(dict.fromkeys(term for term in KEYWORD_SPLIT_RE.split(value.strip()) if term))


def topic_summary(topic: PlanetTopic) -> dict:
    return {"topic_id": topic.topic_id, "title": topic.title, "content": topic.content,
            "author": topic.author, "published_at": topic.published_at, "source_url": topic.source_url,
            "tags": json.loads(topic.tags or "[]")}


@app.get("/", include_in_schema=False)
def home():
    return HTMLResponse(r"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'><title>知识星球股票观点分析</title>
    <style>
    body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:32px auto;max-width:1180px;color:#17233b;background:#f7f9fc}h1,h2{margin:0 0 14px}.card{background:#fff;border:1px solid #e5eaf2;border-radius:12px;padding:22px;margin:18px 0;box-shadow:0 1px 2px #dfe6f033}.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:center}input{padding:10px;border:1px solid #cbd5e1;border-radius:7px;font-size:14px}input[type=text]{min-width:280px}button,.button{padding:10px 15px;border:0;border-radius:7px;background:#1677ff;color:#fff;cursor:pointer;font-size:14px}button.secondary{background:#e8eef8;color:#29415f}.muted{color:#64748b;font-size:13px}.topic{padding:16px 0;border-bottom:1px solid #edf1f5}.topic:last-child{border-bottom:0}.topic-title{font-size:16px;font-weight:650;color:#17233b;cursor:pointer}.topic-title:hover{color:#1677ff}.meta{font-size:13px;color:#64748b;margin:7px 0}.preview{white-space:pre-wrap;line-height:1.6;color:#334155}.tag{display:inline-block;margin:3px 5px 0 0;padding:2px 7px;border-radius:12px;background:#e9f2ff;color:#2769b6;font-size:12px}table{border-collapse:collapse;width:100%;font-size:14px}td,th{padding:9px;border-bottom:1px solid #e8edf3;text-align:left}.pager{display:flex;gap:10px;align-items:center;margin-top:16px}dialog{border:0;border-radius:12px;width:min(780px,90vw);max-height:82vh;box-shadow:0 16px 50px #17233b66;padding:0}dialog::backdrop{background:#17233b77}.modal-head{display:flex;justify-content:space-between;gap:20px;padding:20px 22px;border-bottom:1px solid #e8edf3}.modal-body{padding:20px 22px;white-space:pre-wrap;line-height:1.7;overflow:auto;max-height:62vh}.close{background:transparent;color:#475569;font-size:22px;padding:0}.details{margin-top:16px;padding-top:12px;border-top:1px solid #e8edf3}.error{color:#c2410c}</style>
    <body><h1>知识星球股票观点分析</h1><p class='muted'>主题按知识星球发布时间倒序；数据库保存的是接口返回的发布时间，不是本地同步时间。 · <a href='/kline'>打开 K 线查询</a></p>
    <section class='card'><h2>知识星球主题查询</h2><div class='controls'><input id='topic-keywords' type='text' placeholder='包含全部关键词，例如：深信服 翻倍'><button id='search-topics'>查询</button><button id='clear-topics' class='secondary'>清空</button></div><p id='topic-status' class='muted'></p><div id='topic-list'></div><div id='pager' class='pager'></div></section>
    <section class='card'><h2>关键词统计</h2><p class='muted'>“未来 1 月”指发布事件日后的 20 个交易日；只有完整取得 20 个交易日行情的样本才参与涨幅≥10%排行。</p><div class='controls'><input id='stat-keyword' placeholder='关键词'><input id='stat-stock' placeholder='股票代码'><input id='stat-from' type='date'><input id='stat-to' type='date'><button id='load-stats'>刷新统计</button><a class='button' href='/api/export/keywords.csv'>导出 CSV</a></div><table><thead><tr><th>关键词</th><th>主题数</th><th>股票数</th><th>未来1月平均收益</th><th>1月涨幅≥10%比例</th><th>有效样本</th></tr></thead><tbody id='stat-rows'></tbody></table></section>
    <dialog id='topic-modal'><div class='modal-head'><div><strong id='modal-title'></strong><div id='modal-meta' class='meta'></div></div><button id='close-modal' class='close' aria-label='关闭'>×</button></div><div id='modal-body' class='modal-body'></div></dialog>
    <script>
    const state={page:1,pageSize:20}; const $=id=>document.getElementById(id);
    const bjt=value=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(new Date(value));
    const add=(parent,tag,text,className='')=>{const el=document.createElement(tag);el.textContent=text;if(className)el.className=className;parent.append(el);return el};
    const tags=(parent,values)=>values.forEach(value=>add(parent,'span','#'+value,'tag'));
    const preview=value=>value.replace(/\s+/g,' ').slice(0,180)+(value.replace(/\s+/g,' ').length>180?'…':'');
    async function loadTopics(page=1){const q=new URLSearchParams({page:String(page),page_size:String(state.pageSize)});const keywords=$('topic-keywords').value.trim();if(keywords)q.set('keywords',keywords);const res=await fetch('/api/topics/search?'+q);const data=await res.json();if(!res.ok)throw new Error(data.detail||'查询失败');state.page=data.page;const list=$('topic-list');list.replaceChildren();$('topic-status').className='muted';$('topic-status').textContent=`共 ${data.total} 条${data.keywords.length?'；同时包含：'+data.keywords.join('、'):''}`;if(!data.items.length){add(list,'p','没有符合条件的主题。','muted')}data.items.forEach(topic=>{const row=document.createElement('article');row.className='topic';const title=add(row,'div',topic.title||'（无标题）','topic-title');title.onclick=()=>showTopic(topic.topic_id).catch(showError);add(row,'div',`${bjt(topic.published_at)} · ${topic.author||'未知作者'}`,'meta');add(row,'div',preview(topic.content),'preview');tags(row,topic.tags||[]);list.append(row)});const pager=$('pager');pager.replaceChildren();const pages=Math.max(1,Math.ceil(data.total/data.page_size));const prev=add(pager,'button','上一页','secondary');prev.disabled=data.page<=1;prev.onclick=()=>loadTopics(data.page-1).catch(showError);add(pager,'span',`第 ${data.page} / ${pages} 页`,'muted');const pageInput=document.createElement('input');pageInput.type='number';pageInput.min='1';pageInput.max=String(pages);pageInput.value=String(data.page);pageInput.style.width='64px';pageInput.setAttribute('aria-label','跳转页码');pager.append(pageInput);const jump=add(pager,'button','跳转','secondary');const go=()=>{const target=Number(pageInput.value);if(!Number.isInteger(target)||target<1||target>pages){throw new Error(`请输入 1 到 ${pages} 的页码`)}return loadTopics(target)};jump.onclick=()=>go().catch(showError);pageInput.onkeydown=event=>{if(event.key==='Enter')go().catch(showError)};const next=add(pager,'button','下一页','secondary');next.disabled=data.page>=pages;next.onclick=()=>loadTopics(data.page+1).catch(showError)}
    async function showTopic(id){const res=await fetch('/api/topics/'+encodeURIComponent(id));const topic=await res.json();if(!res.ok)throw new Error(topic.detail||'无法读取主题');$('modal-title').textContent=topic.title||'（无标题）';$('modal-meta').textContent=`知识星球发布时间：${bjt(topic.published_at)} · ${topic.author||'未知作者'}`;const body=$('modal-body');body.replaceChildren();tags(body,topic.tags||[]);add(body,'div',topic.content||'（无正文）','preview');const details=document.createElement('div');details.className='details';add(details,'div','已识别股票：'+(topic.stocks.map(x=>`${x.code} ${x.name}`.trim()).join('、')||'无'));add(details,'div','已识别关键词：'+(topic.keywords.map(x=>x.keyword).join('、')||'无'));body.append(details);$('topic-modal').showModal()}
    async function loadStats(){const q=new URLSearchParams();[['stat-keyword','keyword'],['stat-stock','stock_code'],['stat-from','start_date'],['stat-to','end_date']].forEach(([id,key])=>{const value=$(id).value;if(value)q.set(key,value)});const res=await fetch('/api/stats/keywords?'+q);const data=await res.json();const rows=$('stat-rows');rows.replaceChildren();data.forEach(item=>{const row=document.createElement('tr');[item.keyword,item.topic_count,item.stock_count,item.avg_return_20d==null?'-':(item.avg_return_20d*100).toFixed(2)+'%',item.rise_rate_10pct_20d==null?'-':(item.rise_rate_10pct_20d*100).toFixed(2)+'%',`${item.eligible_count_20d} / ${item.sample_sufficient?'充足':'不足10篇'}`].forEach(value=>add(row,'td',String(value)));rows.append(row)})}
    $('search-topics').onclick=()=>loadTopics(1).catch(showError);$('clear-topics').onclick=()=>{$('topic-keywords').value='';loadTopics(1).catch(showError)};$('topic-keywords').onkeydown=event=>{if(event.key==='Enter')loadTopics(1).catch(showError)};$('close-modal').onclick=()=>$('topic-modal').close();function showError(error){$('topic-status').textContent=error.message;$('topic-status').className='error'}$('load-stats').onclick=()=>loadStats().catch(showError);loadTopics().catch(showError);loadStats().catch(showError);
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
    return {"provider": "akshare", "total": len(rows), "imported": imported, "updated": updated}


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
    codes = request.stock_codes or list(db.scalars(select(Stock.stock_code).join(TopicStock).distinct()))
    imported = skipped = no_data = invalid_rows = 0
    failed_codes: list[str] = []
    try:
        for code in codes:
            # A single code/provider response must not abort the whole batch.
            # Missing trading days are naturally omitted by AKShare; available
            # rows are still imported and the next code continues normally.
            try:
                rows = fetch_akshare_quotes(code, start_date, end_date, request.adjust_type)
            except Exception as exc:
                failed_codes.append(code)
                log.warning("quote fetch skipped code=%s (%s)", code, type(exc).__name__)
                continue
            if not rows:
                no_data += 1
                continue
            stock = db.scalar(select(Stock).where(Stock.stock_code == code))
            if not stock:
                stock = Stock(stock_code=code, exchange="SH" if code.startswith("6") else "SZ")
                db.add(stock)
                db.flush()
            for row in rows:
                try:
                    trade_date = date.fromisoformat(row["date"])
                except (KeyError, TypeError, ValueError):
                    invalid_rows += 1
                    log.warning("quote row skipped code=%s (invalid date)", code)
                    continue
                exists = db.scalar(select(StockDailyQuote).where(StockDailyQuote.stock_id == stock.id,
                                                                  StockDailyQuote.trade_date == trade_date,
                                                                  StockDailyQuote.adjust_type == request.adjust_type))
                if exists:
                    skipped += 1
                    continue
                db.add(StockDailyQuote(stock_id=stock.id, trade_date=trade_date,
                                       open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                                       volume=row["volume"], amount=row["amount"],
                                       turnover_rate=row["turnover_rate"], adjust_type=request.adjust_type))
                imported += 1
        db.commit()
    except RuntimeError as exc:
        db.rollback()
        raise HTTPException(503, str(exc)) from exc
    except (ValueError, KeyError) as exc:
        db.rollback()
        raise HTTPException(400, f"invalid market data: {exc}") from exc
    return {"provider": "akshare", "imported": imported, "skipped": skipped, "no_data": no_data,
            "invalid_rows": invalid_rows, "failed": len(failed_codes), "failed_codes": failed_codes}


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
        "items": items,
    }


@app.get("/api/stocks/mentioned")
def mentioned_stock_codes(db: Session = Depends(db_session)):
    """Codes referenced by at least one Knowledge Planet topic."""
    return list(db.scalars(select(Stock.stock_code).join(TopicStock).distinct().order_by(Stock.stock_code)))


@app.post("/api/analyze/rebuild")
def analyze(db: Session = Depends(db_session)):
    rebuild_returns(db)
    db.commit()
    return {"status": "ok"}


@app.get("/api/stats/keywords", response_model=list[KeywordStat])
def stats(keyword: str | None = None, stock_code: str | None = None,
          start_date: date | None = None, end_date: date | None = None,
          db: Session = Depends(db_session)):
    return keyword_stats(db, keyword_filter=keyword, stock_code=stock_code,
                         start_date=start_date, end_date=end_date)


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
            keyword = Keyword(keyword=raw, normalized_keyword=raw)
            db.add(keyword)
            db.flush()
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
