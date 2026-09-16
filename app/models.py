from datetime import date, datetime
from decimal import Decimal
from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


class PlanetCircle(Base):
    __tablename__ = "planet_circle"
    id: Mapped[int] = mapped_column(primary_key=True)
    circle_id: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200))


class PlanetTopic(Base):
    __tablename__ = "planet_topic"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(100), unique=True)
    title: Mapped[str] = mapped_column(String(500), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(200), default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    stocks = relationship("TopicStock", back_populates="topic", cascade="all, delete-orphan")
    keywords = relationship("TopicKeyword", back_populates="topic", cascade="all, delete-orphan")


class Stock(Base):
    __tablename__ = "stock"
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_code: Mapped[str] = mapped_column(String(20), unique=True)
    stock_name: Mapped[str] = mapped_column(String(100), default="")
    exchange: Mapped[str] = mapped_column(String(20), default="")
    industry: Mapped[str] = mapped_column(String(100), default="")


class TopicStock(Base):
    __tablename__ = "topic_stock"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("planet_topic.id"))
    stock_id: Mapped[int] = mapped_column(ForeignKey("stock.id"))
    mention_context: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), default=Decimal("1"))
    topic = relationship("PlanetTopic", back_populates="stocks")
    stock = relationship("Stock")
    __table_args__ = (UniqueConstraint("topic_id", "stock_id"),)


class Keyword(Base):
    __tablename__ = "keyword"
    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String(100))
    normalized_keyword: Mapped[str] = mapped_column(String(100), unique=True)
    category: Mapped[str] = mapped_column(String(50), default="general")


class TopicKeyword(Base):
    __tablename__ = "topic_keyword"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("planet_topic.id"))
    keyword_id: Mapped[int] = mapped_column(ForeignKey("keyword.id"))
    context: Mapped[str] = mapped_column(Text, default="")
    topic = relationship("PlanetTopic", back_populates="keywords")
    keyword = relationship("Keyword")
    __table_args__ = (UniqueConstraint("topic_id", "keyword_id"),)


class StockDailyQuote(Base):
    __tablename__ = "stock_daily_quote"
    id: Mapped[int] = mapped_column(primary_key=True)
    stock_id: Mapped[int] = mapped_column(ForeignKey("stock.id"))
    trade_date: Mapped[date] = mapped_column(Date)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 4), default=0)
    turnover_rate: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0)
    adjust_type: Mapped[str] = mapped_column(String(10), default="qfq")
    __table_args__ = (UniqueConstraint("stock_id", "trade_date", "adjust_type"),)


class StockEventReturn(Base):
    __tablename__ = "stock_event_return"
    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("planet_topic.id"))
    stock_id: Mapped[int] = mapped_column(ForeignKey("stock.id"))
    event_date: Mapped[date] = mapped_column(Date)
    return_1d: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    return_3d: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    return_5d: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    return_10d: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    max_return_5d: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    rise_10pct_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("topic_id", "stock_id"),)


class SyncJob(Base):
    __tablename__ = "sync_job"
    id: Mapped[int] = mapped_column(primary_key=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")
    error_message: Mapped[str] = mapped_column(Text, default="")
