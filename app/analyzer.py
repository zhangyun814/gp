from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import jieba
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Keyword, PlanetTopic, Stock, StockDailyQuote, StockEventReturn, TopicKeyword, TopicStock


KEYWORDS = {
    "业绩": "业绩", "业绩预增": "业绩预增", "订单": "订单", "订单增长": "订单",
    "涨价": "涨价", "产品涨价": "涨价", "扩产": "扩产", "产能投放": "产能投放",
    "回购": "回购", "增持": "增持", "并购": "并购", "中标": "中标", "政策": "政策",
    "出口": "出口", "需求": "需求", "库存": "库存", "盈利": "盈利", "景气度": "景气度",
    "供给收缩": "供给收缩",
}

# Editable seed aliases; a licensed stock master can replace this map later.
STOCK_ALIASES = {
    "贵州茅台": ("600519", "贵州茅台"), "卓胜微": ("300782", "卓胜微"), "唯捷创芯": ("688153", "唯捷创芯"),
    "仕佳光子": ("688313", "仕佳光子"), "仕佳": ("688313", "仕佳光子"), "长光华芯": ("688048", "长光华芯"),
    "长光": ("688048", "长光华芯"), "源杰科技": ("688498", "源杰科技"), "源杰": ("688498", "源杰科技"),
    "永鼎股份": ("600105", "永鼎股份"), "永鼎": ("600105", "永鼎股份"), "光迅科技": ("002281", "光迅科技"),
    "光迅": ("002281", "光迅科技"), "天孚通信": ("300394", "天孚通信"), "天孚": ("300394", "天孚通信"),
    "光库科技": ("300620", "光库科技"), "光库": ("300620", "光库科技"), "东田微": ("301183", "东田微"),
    "强瑞技术": ("301128", "强瑞技术"), "潍柴动力": ("000338", "潍柴动力"), "恒立液压": ("601100", "恒立液压"),
    "华丰科技": ("688629", "华丰科技"), "昌红科技": ("300151", "昌红科技"), "会稽山": ("601579", "会稽山"),
    "沃尔德": ("688028", "沃尔德"), "高澜股份": ("300499", "高澜股份"), "高澜": ("300499", "高澜股份"),
}
CODE_RE = re.compile(r"(?<!\d)(?:(?:SH|SZ)[.：:]?)?([036]\d{5})(?!\d)", re.I)
BJT = ZoneInfo("Asia/Shanghai")


def exchange_for(code: str) -> str:
    return "SH" if code.startswith(("6", "68")) else "SZ"


def context(text: str, term: str, width: int = 80) -> str:
    pos = text.find(term)
    return text[max(0, pos - width // 2):pos + len(term) + width // 2] if pos >= 0 else text[:width]


def keyword_matches(text: str, tokens: set[str]) -> dict[str, str]:
    matches: dict[str, str] = {}
    for raw, normalized in KEYWORDS.items():
        if raw in text or raw in tokens:
            matches.setdefault(normalized, raw)
    return matches


def _stock_record(db: Session, code: str, name: str = "") -> Stock:
    stock = db.scalar(select(Stock).where(Stock.stock_code == code))
    if not stock:
        stock = Stock(stock_code=code, stock_name=name, exchange=exchange_for(code))
        db.add(stock)
        db.flush()
    elif name and not stock.stock_name:
        stock.stock_name = name
    return stock


def extract_topic(db: Session, topic: PlanetTopic, stock_catalog: list[Stock] | None = None):
    text = f"{topic.title} {topic.content}"
    tokens = set(jieba.lcut(text))
    matches: dict[str, tuple[str, float, str]] = {}
    for match in CODE_RE.finditer(text):
        code = match.group(1)
        matches[code] = (code, 1.0, match.group(0))
    for alias, (code, name) in STOCK_ALIASES.items():
        if alias in text:
            matches.setdefault(code, (name, 0.9, alias))
    for stock in stock_catalog if stock_catalog is not None else list(db.scalars(select(Stock).where(Stock.stock_name != ""))):
        if stock.stock_name in text:
            matches.setdefault(stock.stock_code, (stock.stock_name, 0.85, stock.stock_name))
    for code, (name, confidence, term) in matches.items():
        stock = _stock_record(db, code, name if not name.isdigit() else "")
        if not db.scalar(select(TopicStock).where(TopicStock.topic_id == topic.id, TopicStock.stock_id == stock.id)):
            db.add(TopicStock(topic_id=topic.id, stock_id=stock.id,
                              mention_context=context(text, term), confidence=Decimal(str(confidence))))
    for normalized, raw in keyword_matches(text, tokens).items():
        keyword = db.scalar(select(Keyword).where(Keyword.normalized_keyword == normalized))
        if not keyword:
            keyword = Keyword(keyword=normalized, normalized_keyword=normalized)
            db.add(keyword)
            db.flush()
        if not db.scalar(select(TopicKeyword).where(TopicKeyword.topic_id == topic.id, TopicKeyword.keyword_id == keyword.id)):
            db.add(TopicKeyword(topic_id=topic.id, keyword_id=keyword.id, context=context(text, raw)))


def _published_local(published_at: datetime) -> datetime:
    stored = published_at.replace(tzinfo=timezone.utc) if published_at.tzinfo is None else published_at
    return stored.astimezone(BJT)


def event_trade_date(db: Session, stock_id: int, published_at: datetime) -> date | None:
    local = _published_local(published_at)
    first_date = local.date() + timedelta(days=1) if local.time() >= time(15, 0) else local.date()
    row = db.scalar(select(StockDailyQuote).where(StockDailyQuote.stock_id == stock_id,
                                                   StockDailyQuote.trade_date >= first_date)
                    .order_by(StockDailyQuote.trade_date).limit(1))
    return row.trade_date if row else None


def next_quotes(db: Session, stock_id: int, event_date: date):
    return list(db.scalars(select(StockDailyQuote).where(StockDailyQuote.stock_id == stock_id,
                                                        StockDailyQuote.trade_date > event_date)
                           .order_by(StockDailyQuote.trade_date).limit(20)))


def base_quote(db: Session, stock_id: int, event_date: date):
    return db.scalar(select(StockDailyQuote).where(StockDailyQuote.stock_id == stock_id,
                                                   StockDailyQuote.trade_date == event_date)
                     .order_by(StockDailyQuote.trade_date).limit(1))


def return_metrics(base: Decimal, quotes) -> dict[str, Decimal | None]:
    def ret(days: int):
        return Decimal(str(quotes[days - 1].close)) / base - 1 if len(quotes) >= days else None

    def max_ret(days: int):
        return max(Decimal(str(row.close)) / base - 1 for row in quotes[:days]) if len(quotes) >= days else None

    return {"return_1d": ret(1), "return_3d": ret(3), "return_5d": ret(5), "return_10d": ret(10),
            "return_20d": ret(20), "max_return_5d": max_ret(5), "max_return_20d": max_ret(20)}


def rebuild_returns(db: Session):
    db.execute(delete(StockEventReturn))
    rows = db.execute(select(PlanetTopic, TopicStock).join(TopicStock, TopicStock.topic_id == PlanetTopic.id)).all()
    for topic, link in rows:
        event_date = event_trade_date(db, link.stock_id, topic.published_at)
        if not event_date:
            continue
        base_quote_row = base_quote(db, link.stock_id, event_date)
        quotes = next_quotes(db, link.stock_id, event_date)
        if not base_quote_row or not quotes:
            continue
        base = Decimal(str(base_quote_row.close))

        metrics = return_metrics(base, quotes)
        db.add(StockEventReturn(topic_id=topic.id, stock_id=link.stock_id, event_date=event_date,
                                **metrics,
                                rise_10pct_flag=bool(metrics["max_return_5d"] is not None and metrics["max_return_5d"] >= Decimal(str(settings.rise_threshold))),
                                rise_10pct_20d_flag=bool(metrics["max_return_20d"] is not None and metrics["max_return_20d"] >= Decimal(str(settings.rise_threshold)))))


def keyword_stats(db: Session, keyword_filter: str | None = None, stock_code: str | None = None,
                  start_date: date | None = None, end_date: date | None = None):
    result = []
    keywords = list(db.scalars(select(Keyword).order_by(Keyword.normalized_keyword)))
    if keyword_filter:
        needle = keyword_filter.lower()
        keywords = [keyword for keyword in keywords if needle in keyword.keyword.lower() or needle in keyword.normalized_keyword.lower()]
    for keyword in keywords:
        statement = (select(StockEventReturn, PlanetTopic, Stock)
                     .join(PlanetTopic, PlanetTopic.id == StockEventReturn.topic_id)
                     .join(Stock, Stock.id == StockEventReturn.stock_id)
                     .join(TopicKeyword, TopicKeyword.topic_id == StockEventReturn.topic_id)
                     .where(TopicKeyword.keyword_id == keyword.id))
        if stock_code:
            statement = statement.where(Stock.stock_code == stock_code)
        if start_date:
            statement = statement.where(StockEventReturn.event_date >= start_date)
        if end_date:
            statement = statement.where(StockEventReturn.event_date <= end_date)
        rows = db.execute(statement).all()
        valid = [row[0] for row in rows if row[0].max_return_5d is not None]
        valid_5d = [row for row in valid if row.return_5d is not None]
        valid_20d = [row[0] for row in rows if row[0].max_return_20d is not None]
        return_20d = [row for row in valid_20d if row.return_20d is not None]
        topic_ids = {row[1].id for row in rows}
        stock_ids = {row[2].id for row in rows}
        result.append({"keyword": keyword.keyword, "topic_count": len(topic_ids), "stock_count": len(stock_ids),
                       "avg_return_5d": float(sum(row.return_5d for row in valid_5d) / len(valid_5d)) if valid_5d else None,
                       "rise_rate_10pct": sum(bool(row.rise_10pct_flag) for row in valid) / len(valid) if valid else None,
                       "avg_return_20d": float(sum(row.return_20d for row in return_20d) / len(return_20d)) if return_20d else None,
                       "rise_rate_10pct_20d": sum(bool(row.rise_10pct_20d_flag) for row in valid_20d) / len(valid_20d) if valid_20d else None,
                       "eligible_count_20d": len(valid_20d), "sample_sufficient": len(valid_20d) >= 10})
    return sorted(result, key=lambda row: (row["sample_sufficient"], row["rise_rate_10pct_20d"] or -1), reverse=True)
