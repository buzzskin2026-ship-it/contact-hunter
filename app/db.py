from __future__ import annotations

from collections.abc import Generator
from threading import Lock

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

_schema_lock = Lock()
_schema_initialized = False


def init_db() -> None:
    """Create the schema once, including when a platform skips the lifespan hook."""
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if _schema_initialized:
            return
        # Importing the models registers all mapped tables in Base.metadata.
        from app import models as _models  # noqa: F401

        Base.metadata.create_all(bind=engine)
        _schema_initialized = True


def get_db() -> Generator[Session, None, None]:
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
