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
