from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.types import JSON

# Prefer JSONB on Postgres; fall back to generic JSON for other dialects in tests.
JsonType = JSON().with_variant(JSONB(), "postgresql")


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    requester_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    chat_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)

    tasks: Mapped[list[TaskRow]] = relationship(back_populates="job", cascade="all, delete-orphan")


class TaskRow(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(String(64), index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    inputs: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    result: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    chat_id: Mapped[str] = mapped_column(String(128), default="")
    requester_id: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)

    job: Mapped[JobRow] = relationship(back_populates="tasks")


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    task_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    agent_id: Mapped[str] = mapped_column(String(64), default="")
    event: Mapped[str] = mapped_column(String(128), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)


class UserOAuthRow(Base):
    __tablename__ = "user_oauth"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    requester_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), default="doc")
    access_token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)


def default_database_url() -> str:
    return os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://agent:agent@127.0.0.1:5432/agent_platform",
    )


def create_db_engine(database_url: str | None = None):
    url = database_url or default_database_url()
    return create_engine(url, pool_pre_ping=True, future=True)


def create_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ping_db(engine) -> bool:
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return True
