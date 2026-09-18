from datetime import date, datetime
from pydantic import BaseModel, Field


class TopicIn(BaseModel):
    topic_id: str = Field(min_length=1, max_length=100)
    title: str = ""
    content: str = ""
    author: str = ""
    published_at: datetime
    source_url: str = ""
    tags: list[str] = Field(default_factory=list)


class TopicImportResult(BaseModel):
    imported: int
    skipped: int


class ZsxqSyncIn(BaseModel):
    group_id: str = Field(min_length=1, max_length=100)
    group_name: str = ""
    scope: str = "digests"
    topics: list[TopicIn] = Field(default_factory=list)


class ZsxqSyncResult(TopicImportResult):
    job_id: int
    received: int


class KeywordStat(BaseModel):
    keyword: str
    topic_count: int
    stock_count: int
    avg_return_5d: float | None
    rise_rate_10pct: float | None
    avg_return_20d: float | None
    rise_rate_10pct_20d: float | None
    eligible_count_20d: int
    sample_sufficient: bool


class QuoteSyncIn(BaseModel):
    stock_codes: list[str] = Field(default_factory=list)
    start_date: date | None = None
    end_date: date | None = None
    adjust_type: str = "qfq"
    max_attempts: int = Field(default=3, ge=1, le=5)


class StockAnnotationIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = ""
    context: str = ""
    confidence: float = Field(default=1.0, ge=0, le=1)


class TopicAnnotationsIn(BaseModel):
    stocks: list[StockAnnotationIn] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class ManualKeywordIn(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)


class ManualKeywordActiveIn(BaseModel):
    active: bool
