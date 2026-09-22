from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db():
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    # create_all does not alter an existing Docker volume. Keep this tiny
    # migration for the one additive field introduced by topic sync.
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE planet_topic ADD COLUMN IF NOT EXISTS tags TEXT NOT NULL DEFAULT '[]'"))
            connection.execute(text("ALTER TABLE stock_event_return ADD COLUMN IF NOT EXISTS return_20d NUMERIC(12, 6)"))
            connection.execute(text("ALTER TABLE stock_event_return ADD COLUMN IF NOT EXISTS max_return_20d NUMERIC(12, 6)"))
            connection.execute(text("ALTER TABLE stock_event_return ADD COLUMN IF NOT EXISTS rise_10pct_20d_flag BOOLEAN NOT NULL DEFAULT FALSE"))
            connection.execute(text("ALTER TABLE keyword ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE"))
            connection.execute(text("ALTER TABLE keyword ADD COLUMN IF NOT EXISTS deleted BOOLEAN NOT NULL DEFAULT FALSE"))
            # The unique index starts with topic_id, so it cannot efficiently
            # validate/delete rows by keyword_id alone during a full rescan.
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_topic_keyword_keyword_id ON topic_keyword (keyword_id)"))
