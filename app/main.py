import csv
import io
import logging
from datetime import date
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from .analyzer import extract_topic, keyword_stats, rebuild_returns
from .db import SessionLocal, init_db
from .models import PlanetTopic, Stock, StockDailyQuote
from .schemas import KeywordStat, TopicImportResult, TopicIn

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("planet-stock")


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="知识星球观点分析", version="0.1.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
def home():
    return HTMLResponse("""<!doctype html><meta charset='utf-8'><title>股票观点关键词分析</title>
    <style>body{font-family:system-ui;margin:40px;color:#17233b}table{border-collapse:collapse;width:100%}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}button{padding:8px 14px}</style>
    <h1>知识星球股票观点关键词分析</h1>
    <p>先通过 <a href='/docs'>API 文档</a> 导入主题和行情，再点击刷新统计。</p>
    <button onclick='load()'>刷新关键词统计</button><p id='status'></p>
    <table><thead><tr><th>关键词</th><th>主题数</th><th>股票数</th><th>未来5日平均收益</th><th>涨幅≥10%比例</th><th>样本</th></tr></thead><tbody id='rows'></tbody></table>
    <script>async function load(){const r=await fetch('/api/stats/keywords');const d=await r.json();document.getElementById('rows').innerHTML=d.map(x=>`<tr><td>${x.keyword}</td><td>${x.topic_count}</td><td>${x.stock_count}</td><td>${x.avg_return_5d==null?'-':(x.avg_return_5d*100).toFixed(2)+'%'}</td><td>${x.rise_rate_10pct==null?'-':(x.rise_rate_10pct*100).toFixed(2)+'%'}</td><td>${x.sample_sufficient?'充足':'不足10篇'}</td></tr>`).join('');document.getElementById('status').textContent='已刷新';}load()</script>""")


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/api/topics/import", response_model=TopicImportResult)
def import_topics(topics: list[TopicIn], db: Session = Depends(db_session)):
    imported = skipped = 0
    for item in topics:
        if db.scalar(select(PlanetTopic).where(PlanetTopic.topic_id == item.topic_id)):
            skipped += 1
            continue
        topic = PlanetTopic(**item.model_dump())
        db.add(topic); db.flush()
        extract_topic(db, topic)
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": skipped}


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


@app.post("/api/analyze/rebuild")
def analyze(db: Session = Depends(db_session)):
    rebuild_returns(db)
    db.commit()
    return {"status": "ok"}


@app.get("/api/stats/keywords", response_model=list[KeywordStat])
def stats(db: Session = Depends(db_session)):
    return keyword_stats(db)


@app.get("/api/topics")
def topics(limit: int = 50, db: Session = Depends(db_session)):
    if limit < 1 or limit > 200:
        raise HTTPException(400, "limit must be between 1 and 200")
    return [{"topic_id": t.topic_id, "title": t.title, "published_at": t.published_at, "source_url": t.source_url}
            for t in db.scalars(select(PlanetTopic).order_by(PlanetTopic.published_at.desc()).limit(limit))]
