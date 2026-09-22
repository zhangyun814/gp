from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import jieba
from sqlalchemy import delete, select, text
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
MANUAL_KEYWORD_CATEGORY = "manual"

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
AUTO_KEYWORD_CATEGORY = "auto"
AUTO_KEYWORD_MIN_TOPICS = 5
AUTO_KEYWORD_MIN_SAMPLES = 10
AUTO_KEYWORD_MAX_TERMS = 200
AUTO_KEYWORD_MAX_PER_TOPIC = 30
EMPHASIS_PATTERNS = tuple(re.compile(pattern) for pattern in (
    r"(?:继续|持续|坚定|强烈|重点|反复|核心|明确|再次|长期|积极|高度|务必)(?:看好|看多|推荐|关注|重视|配置)",
    r"(?:强推|首推|力推|必看|必配|重磅推荐)",
    r"(?:强|重点|继续|反复|明确)call",
    r"(?:大超预期|远超预期|明显超预期|超预期)",
    r"翻(?:\d+(?:\.\d+)?|[一二两三四五六七八九十几数])?倍(?:以上)?(?:空间|潜力)?",
    r"(?<![翻\d一二两三四五六七八九十几数])(?:\d+(?:\.\d+)?|[一二两三四五六七八九十几数])倍(?:以上)?(?:空间|潜力)",
    r"(?:核心|重点|首选|必选)标的",
    r"(?:买入|增持)评级",
    r"(?:上调|大幅上调)(?:评级|目标价|盈利预测)",
    r"(?:高赔率|高胜率|赔率高|胜率高)",
    r"(?:严重|明显|显著|极度)?低估",
    r"(?:高|强|巨大|极大)(?:弹性|确定性)",
    r"(?:弹性|空间)(?:巨大|极大|十足)",
    r"(?:重大突破|拐点确立|加速兑现|业绩爆发|爆发式增长|戴维斯双击)",
))


def exchange_for(code: str) -> str:
    return "SH" if code.startswith(("6", "68")) else "SZ"


def context(text: str, term: str, width: int = 80) -> str:
    pos = text.find(term)
    return text[max(0, pos - width // 2):pos + len(term) + width // 2] if pos >= 0 else text[:width]


def keyword_matches(text: str, tokens: set[str], keyword_map: dict[str, str] | None = None) -> dict[str, str]:
    matches: dict[str, str] = {}
    for raw, normalized in (KEYWORDS if keyword_map is None else keyword_map).items():
        if raw in text or raw in tokens:
            matches.setdefault(normalized, raw)
    return matches


def ensure_manual_keywords(db: Session) -> int:
    """Seed the editable dictionary once, preserving user changes and status.
    Words the user deleted are tombstoned (deleted=True) and not re-created."""
    added = changed = 0
    for normalized in dict.fromkeys(KEYWORDS.values()):
        keyword = db.scalar(select(Keyword).where(Keyword.normalized_keyword == normalized))
        if keyword is None:
            db.add(Keyword(keyword=normalized, normalized_keyword=normalized,
                           category=MANUAL_KEYWORD_CATEGORY, active=True))
            added += 1
        elif keyword.deleted:
            continue
        elif keyword.category != AUTO_KEYWORD_CATEGORY and keyword.category == "general":
            keyword.category = MANUAL_KEYWORD_CATEGORY
            changed += 1
    if added or changed:
        db.commit()
    return added


def candidate_keywords(text: str, _stock_names: set[str] | None = None) -> set[str]:
    """Extract recommendation strength and upside language, never company nouns."""
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).lower())
    return {match.group(0) for pattern in EMPHASIS_PATTERNS for match in pattern.finditer(normalized)}


def discover_auto_keywords(db: Session, min_topics: int = AUTO_KEYWORD_MIN_TOPICS) -> dict:
    """Mine recurring emphasis terms, replacing previously generated links."""
    stock_names = {stock.stock_name for stock in db.scalars(select(Stock)) if stock.stock_name}
    removed = db.execute(text("""
        DELETE FROM topic_keyword AS tk
        USING keyword AS k
        WHERE tk.keyword_id = k.id
          AND k.category = :category
          AND tk.context <> '人工标注'
    """), {"category": AUTO_KEYWORD_CATEGORY}).rowcount or 0
    db.execute(text("""
        DELETE FROM keyword AS k
        WHERE k.category = :category
          AND NOT EXISTS (
              SELECT 1 FROM topic_keyword AS tk WHERE tk.keyword_id = k.id
          )
    """), {"category": AUTO_KEYWORD_CATEGORY})
    db.flush()
    frequencies: Counter[str] = Counter()
    topics_scanned = 0
    last_topic_id = 0
    while True:
        topic_batch = list(db.scalars(select(PlanetTopic)
                                      .where(PlanetTopic.id > last_topic_id)
                                      .order_by(PlanetTopic.id).limit(500)))
        if not topic_batch:
            break
        for topic in topic_batch:
            topics_scanned += 1
            topic_text = f"{topic.title} {topic.content}"
            frequencies.update(candidate_keywords(topic_text, stock_names))
        last_topic_id = topic_batch[-1].id
        db.expunge_all()
    eligible_counts = [(term, count) for term, count in frequencies.items() if count >= min_topics]
    eligible = {term for term, _count in sorted(eligible_counts, key=lambda item: (-item[1], item[0]))[:AUTO_KEYWORD_MAX_TERMS]}

    # Keep only scalar ids in this map so flushed batches can be detached
    # from SQLAlchemy's identity map without retaining every ORM object.
    keyword_by_term = {keyword.normalized_keyword: (keyword.id, keyword.category)
                       for keyword in db.scalars(select(Keyword))}
    existing_links = {(link.topic_id, link.keyword_id) for link in db.scalars(select(TopicKeyword))}
    new_keywords = links_added = 0
    pending = 0
    last_topic_id = 0
    while True:
        topic_batch = list(db.scalars(select(PlanetTopic)
                                      .where(PlanetTopic.id > last_topic_id)
                                      .order_by(PlanetTopic.id).limit(500)))
        if not topic_batch:
            break
        for topic in topic_batch:
            topic_text = f"{topic.title} {topic.content}"
            terms = sorted(candidate_keywords(topic_text, stock_names) & eligible,
                           key=lambda term: (-frequencies[term], term))[:AUTO_KEYWORD_MAX_PER_TOPIC]
            for term in terms:
                keyword_info = keyword_by_term.get(term)
                if not keyword_info:
                    keyword = Keyword(keyword=term, normalized_keyword=term, category=AUTO_KEYWORD_CATEGORY)
                    db.add(keyword)
                    db.flush()
                    keyword_info = (keyword.id, AUTO_KEYWORD_CATEGORY)
                    keyword_by_term[term] = keyword_info
                    new_keywords += 1
                keyword_id, category = keyword_info
                if category != AUTO_KEYWORD_CATEGORY:
                    continue
                key = (topic.id, keyword_id)
                if key in existing_links:
                    continue
                db.add(TopicKeyword(topic_id=topic.id, keyword_id=keyword_id, context=context(topic_text, term)))
                existing_links.add(key)
                links_added += 1
                pending += 1
                if pending >= 2000:
                    db.flush()
                    db.expunge_all()
                    pending = 0
        if pending:
            db.flush()
            pending = 0
        last_topic_id = topic_batch[-1].id
        db.expunge_all()
    return {"topics_scanned": topics_scanned, "candidate_keywords": len(eligible),
            "new_keywords": new_keywords, "links_added": links_added,
            "stale_links_removed": removed,
            "min_topics": min_topics, "max_terms": AUTO_KEYWORD_MAX_TERMS,
            "max_terms_per_topic": AUTO_KEYWORD_MAX_PER_TOPIC}


def _stock_record(db: Session, code: str, name: str = "", known_codes: set[str] | None = None) -> Stock | None:
    """Return (or create) the Stock for a matched code. Bare code matches with no
    name are only trusted when the code exists in the A-share master (known_codes);
    otherwise they are foreign/invalid codes (e.g. Korean tickers) and skipped."""
    stock = db.scalar(select(Stock).where(Stock.stock_code == code))
    if stock:
        if name and not stock.stock_name:
            stock.stock_name = name
        return stock
    if not name and known_codes is not None and code not in known_codes:
        return None
    stock = Stock(stock_code=code, stock_name=name, exchange=exchange_for(code))
    db.add(stock)
    db.flush()
    return stock


def extract_topic(db: Session, topic: PlanetTopic, stock_catalog: list[Stock] | None = None):
    text = f"{topic.title} {topic.content}"
    tokens = set(jieba.lcut(text))
    matches: dict[str, tuple[str, float, str]] = {}
    catalog = stock_catalog if stock_catalog is not None else list(
        db.scalars(select(Stock).where(Stock.stock_name != "")))
    for match in CODE_RE.finditer(text):
        code = match.group(1)
        matches[code] = (code, 1.0, match.group(0))
    for alias, (code, name) in STOCK_ALIASES.items():
        if alias in text:
            matches.setdefault(code, (name, 0.9, alias))
    for stock in catalog:
        if stock.stock_name in text:
            matches.setdefault(stock.stock_code, (stock.stock_name, 0.85, stock.stock_name))
    known_codes = {stock.stock_code for stock in catalog if stock.stock_name}
    for code, (name, confidence, term) in matches.items():
        stock = _stock_record(db, code, name if not name.isdigit() else "", known_codes)
        if stock is None:
            continue
        if not db.scalar(select(TopicStock).where(TopicStock.topic_id == topic.id, TopicStock.stock_id == stock.id)):
            db.add(TopicStock(topic_id=topic.id, stock_id=stock.id,
                              mention_context=context(text, term), confidence=Decimal(str(confidence))))
    manual_keywords = {keyword.keyword: keyword.normalized_keyword for keyword in db.scalars(
        select(Keyword).where(Keyword.category != AUTO_KEYWORD_CATEGORY, Keyword.active.is_(True)))}
    for normalized, raw in keyword_matches(text, tokens, manual_keywords).items():
        keyword = db.scalar(select(Keyword).where(Keyword.normalized_keyword == normalized))
        if not keyword:
            keyword = Keyword(keyword=normalized, normalized_keyword=normalized,
                              category=MANUAL_KEYWORD_CATEGORY, active=True)
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
    quote_rows = list(db.scalars(select(StockDailyQuote)
                                 .where(StockDailyQuote.adjust_type == "qfq")
                                 .order_by(StockDailyQuote.stock_id, StockDailyQuote.trade_date)))
    quotes_by_stock: defaultdict[int, list[StockDailyQuote]] = defaultdict(list)
    for quote in quote_rows:
        quotes_by_stock[quote.stock_id].append(quote)
    quote_dates_by_stock = {stock_id: [quote.trade_date for quote in quotes]
                            for stock_id, quotes in quotes_by_stock.items()}
    # Query link batches by primary key.  Expunging between batches keeps a
    # rebuild bounded without invalidating an active streaming ORM result.
    link_query = (select(PlanetTopic, TopicStock)
                  .join(TopicStock, TopicStock.topic_id == PlanetTopic.id)
                  .where(TopicStock.id > 0)
                  .order_by(TopicStock.id))
    last_link_id = 0
    while True:
        rows = db.execute(link_query.where(TopicStock.id > last_link_id).limit(2000)).all()
        if not rows:
            break
        rebuilt = []
        for topic, link in rows:
            last_link_id = link.id
            quotes = quotes_by_stock.get(link.stock_id, [])
            if not quotes:
                continue
            local = _published_local(topic.published_at)
            first_date = local.date() + timedelta(days=1) if local.time() >= time(15, 0) else local.date()
            quote_dates = quote_dates_by_stock[link.stock_id]
            event_index = bisect_left(quote_dates, first_date)
            if event_index >= len(quotes):
                continue
            event_quote = quotes[event_index]
            future_quotes = quotes[event_index + 1:event_index + 21]
            if not future_quotes:
                continue
            base = Decimal(str(event_quote.close))
            metrics = return_metrics(base, future_quotes)
            rebuilt.append(StockEventReturn(
                topic_id=topic.id, stock_id=link.stock_id, event_date=event_quote.trade_date,
                **metrics,
                rise_10pct_flag=bool(metrics["max_return_5d"] is not None and metrics["max_return_5d"] >= Decimal(str(settings.rise_threshold))),
                rise_10pct_20d_flag=bool(metrics["max_return_20d"] is not None and metrics["max_return_20d"] >= Decimal(str(settings.rise_threshold))),
            ))
        if rebuilt:
            db.add_all(rebuilt)
            db.flush()
        db.expunge_all()
def keyword_stats(db: Session, keyword_filter: str | None = None, stock_code: str | None = None,
                  start_date: date | None = None, end_date: date | None = None):
    result = []
    keywords = list(db.scalars(select(Keyword).where(
        Keyword.category != AUTO_KEYWORD_CATEGORY, Keyword.active.is_(True)
    ).order_by(Keyword.normalized_keyword)))
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


def discovered_keyword_stats(db: Session, keyword_filter: str | None = None,
                             stock_code: str | None = None,
                             min_samples: int = AUTO_KEYWORD_MIN_SAMPLES):
    """Aggregate persisted auto terms against complete 20-trading-day outcomes."""
    keywords = list(db.scalars(select(Keyword).where(Keyword.category == AUTO_KEYWORD_CATEGORY)
                               .order_by(Keyword.normalized_keyword)))
    if keyword_filter:
        needle = keyword_filter.lower()
        keywords = [item for item in keywords if needle in item.keyword.lower()]
    if not keywords:
        return []

    topic_counts: defaultdict[int, set[int]] = defaultdict(set)
    stock_counts: defaultdict[int, set[int]] = defaultdict(set)
    stock_occurrences: defaultdict[int, Counter[tuple[int, str, str]]] = defaultdict(Counter)
    occurrences = select(Keyword.id, TopicKeyword.topic_id, Stock.id, Stock.stock_code, Stock.stock_name)
    occurrences = (occurrences.join(TopicKeyword, TopicKeyword.keyword_id == Keyword.id)
                   .join(TopicStock, TopicStock.topic_id == TopicKeyword.topic_id)
                   .join(Stock, Stock.id == TopicStock.stock_id)
                   .where(Keyword.category == AUTO_KEYWORD_CATEGORY))
    if stock_code:
        occurrences = occurrences.where(Stock.stock_code == stock_code)
    for keyword_id, topic_id, stock_id, code, name in db.execute(occurrences):
        topic_counts[keyword_id].add(topic_id)
        stock_counts[keyword_id].add(stock_id)
        stock_occurrences[keyword_id][(stock_id, code, name)] += 1

    outcomes: defaultdict[int, list[tuple[int, Decimal | None, Decimal | None, bool]]] = defaultdict(list)
    outcome_query = (select(Keyword.id, StockEventReturn.stock_id, StockEventReturn.return_20d,
                            StockEventReturn.max_return_20d, StockEventReturn.rise_10pct_20d_flag)
                     .join(TopicKeyword, TopicKeyword.keyword_id == Keyword.id)
                     .join(StockEventReturn, StockEventReturn.topic_id == TopicKeyword.topic_id)
                     .join(Stock, Stock.id == StockEventReturn.stock_id)
                     .where(Keyword.category == AUTO_KEYWORD_CATEGORY))
    if stock_code:
        outcome_query = outcome_query.where(Stock.stock_code == stock_code)
    for keyword_id, stock_id, return_20d, max_return_20d, flag in db.execute(outcome_query):
        if max_return_20d is not None:
            outcomes[keyword_id].append((stock_id, return_20d, max_return_20d, bool(flag)))

    baseline = list(db.execute(select(StockEventReturn.rise_10pct_20d_flag)
                              .where(StockEventReturn.max_return_20d.is_not(None))).scalars())
    baseline_rate = sum(bool(flag) for flag in baseline) / len(baseline) if baseline else None
    result = []
    for keyword in keywords:
        rows = outcomes[keyword.id]
        success_count = sum(item[3] for item in rows)
        rate = success_count / len(rows) if rows else None
        returns = [float(item[1]) for item in rows if item[1] is not None]
        max_returns = [float(item[2]) for item in rows if item[2] is not None]
        per_stock: defaultdict[int, list[bool]] = defaultdict(list)
        for stock_id, _return, _max_return, flag in rows:
            per_stock[stock_id].append(flag)
        stock_rates = [sum(flags) / len(flags) for flags in per_stock.values()]
        dedup_rate = sum(stock_rates) / len(stock_rates) if stock_rates else None
        representatives = [{"code": code, "name": name, "topics": count}
                           for (stock_id, code, name), count in stock_occurrences[keyword.id].most_common(3)]
        result.append({
            "keyword_id": keyword.id,
            "keyword": keyword.keyword,
            "topic_count": len(topic_counts[keyword.id]),
            "stock_count": len(stock_counts[keyword.id]),
            "eligible_count_20d": len(rows),
            "success_count_20d": success_count,
            "rise_rate_10pct_20d": rate,
            "avg_return_20d": sum(returns) / len(returns) if returns else None,
            "max_return_20d": max(max_returns) if max_returns else None,
            "min_return_20d": min(max_returns) if max_returns else None,
            "baseline_rise_rate_10pct_20d": baseline_rate,
            "uplift_vs_baseline": rate - baseline_rate if rate is not None and baseline_rate is not None else None,
            "dedup_stock_rise_rate_20d": dedup_rate,
            "dedup_stock_count": len(per_stock),
            "representative_stocks": representatives,
            "sample_sufficient": len(rows) >= min_samples,
        })
    return sorted(result, key=lambda item: (
        item["sample_sufficient"], item["uplift_vs_baseline"] if item["uplift_vs_baseline"] is not None else -1,
        item["rise_rate_10pct_20d"] if item["rise_rate_10pct_20d"] is not None else -1,
        item["eligible_count_20d"]), reverse=True)


def discovered_keyword_detail(db: Session, keyword_id: int, limit: int = 100) -> dict:
    keyword = db.scalar(select(Keyword).where(Keyword.id == keyword_id,
                                               Keyword.category == AUTO_KEYWORD_CATEGORY))
    if not keyword:
        return {}
    topic_rows = db.execute(
        select(PlanetTopic, TopicKeyword)
        .join(TopicKeyword, TopicKeyword.topic_id == PlanetTopic.id)
        .where(TopicKeyword.keyword_id == keyword.id)
        .order_by(PlanetTopic.published_at.desc(), PlanetTopic.id.desc())
        .limit(limit)
    ).all()
    topics = []
    for topic, link in topic_rows:
        stocks = db.execute(select(Stock).join(TopicStock, TopicStock.stock_id == Stock.id)
                            .where(TopicStock.topic_id == topic.id)).scalars().all()
        returns = db.execute(select(StockEventReturn, Stock)
                             .join(Stock, Stock.id == StockEventReturn.stock_id)
                             .where(StockEventReturn.topic_id == topic.id)).all()
        topics.append({
            "topic_id": topic.topic_id, "title": topic.title, "author": topic.author,
            "published_at": topic.published_at, "context": link.context,
            "stocks": [{"code": stock.stock_code, "name": stock.stock_name} for stock in stocks],
            "returns": [{"code": stock.stock_code, "name": stock.stock_name,
                         "return_20d": float(event.return_20d) if event.return_20d is not None else None,
                         "max_return_20d": float(event.max_return_20d) if event.max_return_20d is not None else None,
                         "rise_10pct_20d_flag": event.rise_10pct_20d_flag}
                        for event, stock in returns],
        })
    return {"keyword_id": keyword.id, "keyword": keyword.keyword, "topics": topics}
