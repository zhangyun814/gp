import re
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from .config import settings
from .models import Keyword, PlanetTopic, Stock, StockDailyQuote, StockEventReturn, TopicKeyword, TopicStock

KEYWORDS = {
    "业绩": "业绩", "订单": "订单", "订单增长": "订单增长", "涨价": "涨价", "扩产": "扩产",
    "回购": "回购", "增持": "增持", "并购": "并购", "中标": "中标", "政策": "政策",
    "出口": "出口", "需求": "需求", "库存": "库存", "盈利": "盈利", "景气度": "景气度",
    "供给收缩": "供给收缩", "产能投放": "产能投放", "业绩预增": "业绩预增",
}
CODE_RE = re.compile(r"(?<!\d)([036]\d{5})(?!\d)")


def context(text: str, term: str, width: int = 80) -> str:
    pos = text.find(term)
    return text[max(0, pos - width // 2):pos + len(term) + width // 2] if pos >= 0 else text[:width]


def extract_topic(db: Session, topic: PlanetTopic):
    text = f"{topic.title} {topic.content}"
    codes = set(CODE_RE.findall(text))
    for stock in db.scalars(select(Stock).where(Stock.stock_name != "")):
        if stock.stock_name in text:
            codes.add(stock.stock_code)
    for code in codes:
        stock = db.scalar(select(Stock).where(Stock.stock_code == code))
        if not stock:
            stock = Stock(stock_code=code, stock_name="", exchange="SH" if code.startswith("6") else "SZ")
            db.add(stock); db.flush()
        exists = db.scalar(select(TopicStock).where(TopicStock.topic_id == topic.id, TopicStock.stock_id == stock.id))
        if not exists:
            db.add(TopicStock(topic_id=topic.id, stock_id=stock.id, mention_context=context(text, code)))
    for raw, normalized in KEYWORDS.items():
        if raw not in text:
            continue
        keyword = db.scalar(select(Keyword).where(Keyword.normalized_keyword == normalized))
        if not keyword:
            keyword = Keyword(keyword=raw, normalized_keyword=normalized)
            db.add(keyword); db.flush()
        exists = db.scalar(select(TopicKeyword).where(TopicKeyword.topic_id == topic.id, TopicKeyword.keyword_id == keyword.id))
        if not exists:
            db.add(TopicKeyword(topic_id=topic.id, keyword_id=keyword.id, context=context(text, raw)))


def next_quotes(db: Session, stock_id: int, event_date: date):
    return list(db.scalars(select(StockDailyQuote).where(StockDailyQuote.stock_id == stock_id, StockDailyQuote.trade_date > event_date).order_by(StockDailyQuote.trade_date).limit(10)))


def base_quote(db: Session, stock_id: int, event_date: date):
    return db.scalar(select(StockDailyQuote).where(StockDailyQuote.stock_id == stock_id,
                                                    StockDailyQuote.trade_date <= event_date)
                     .order_by(StockDailyQuote.trade_date.desc()).limit(1))


def rebuild_returns(db: Session):
    db.execute(delete(StockEventReturn))
    rows = db.execute(select(PlanetTopic, TopicStock).join(TopicStock, TopicStock.topic_id == PlanetTopic.id)).all()
    for topic, link in rows:
        event_date = topic.published_at.date()
        base_quote_row = base_quote(db, link.stock_id, event_date)
        quotes = next_quotes(db, link.stock_id, event_date)
        if not base_quote_row or not quotes:
            continue
        base = Decimal(str(base_quote_row.close))
        def ret(n):
            return (Decimal(str(quotes[n - 1].close)) / base - 1) if len(quotes) >= n else None
        returns = {n: ret(n) for n in (1, 3, 5, 10)}
        max5 = max((Decimal(str(q.close)) / base - 1 for q in quotes[:5]), default=None)
        db.add(StockEventReturn(topic_id=topic.id, stock_id=link.stock_id, event_date=event_date,
                                return_1d=returns[1], return_3d=returns[3], return_5d=returns[5], return_10d=returns[10],
                                max_return_5d=max5, rise_10pct_flag=bool(max5 is not None and max5 >= Decimal(str(settings.rise_threshold)))))


def keyword_stats(db: Session):
    result = []
    keywords = list(db.scalars(select(Keyword).order_by(Keyword.normalized_keyword)))
    for keyword in keywords:
        rows = db.execute(select(StockEventReturn).join(TopicKeyword, TopicKeyword.topic_id == StockEventReturn.topic_id).where(TopicKeyword.keyword_id == keyword.id)).scalars().all()
        valid = [r for r in rows if r.max_return_5d is not None]
        valid_5d = [r for r in valid if r.return_5d is not None]
        topic_ids = {r.topic_id for r in rows}
        stock_ids = {r.stock_id for r in rows}
        result.append({"keyword": keyword.keyword, "topic_count": len(topic_ids), "stock_count": len(stock_ids),
                       "avg_return_5d": float(sum(r.return_5d for r in valid_5d) / len(valid_5d)) if valid_5d else None,
                       "rise_rate_10pct": sum(bool(r.rise_10pct_flag) for r in valid) / len(valid) if valid else None,
                       "sample_sufficient": len(topic_ids) >= 10})
    return sorted(result, key=lambda x: (x["sample_sufficient"], x["rise_rate_10pct"] or -1), reverse=True)
