"""SQLAlchemy engine and session dependency."""

from __future__ import annotations

from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False}
    if DATABASE_URL.startswith("sqlite")
    else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def enable_sqlite_foreign_keys(connection: Any, _: Any) -> None:
    if DATABASE_URL.startswith("sqlite"):
        connection.execute("PRAGMA foreign_keys=ON")


def get_db() -> Session:
    with SessionLocal() as session:
        yield session
