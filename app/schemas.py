from datetime import datetime
from pydantic import BaseModel, Field


class TopicIn(BaseModel):
    topic_id: str = Field(min_length=1, max_length=100)
    title: str = ""
    content: str = ""
    author: str = ""
    published_at: datetime
    source_url: str = ""


class TopicImportResult(BaseModel):
    imported: int
    skipped: int


class KeywordStat(BaseModel):
    keyword: str
    topic_count: int
    stock_count: int
    avg_return_5d: float | None
    rise_rate_10pct: float | None
    sample_sufficient: bool
