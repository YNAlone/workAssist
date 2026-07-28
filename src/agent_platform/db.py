from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
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
    __table_args__ = (
        Index(
            "uq_jobs_tenant_chat",
            "tenant_key",
            "chat_id",
            unique=True,
            postgresql_where=text("chat_id <> ''"),
            sqlite_where=text("chat_id <> ''"),
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    tenant_key: Mapped[str] = mapped_column(String(128), default="default", index=True)
    requester_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    chat_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    repo: Mapped[str] = mapped_column(String(512), default="")
    base_branch: Mapped[str] = mapped_column(String(255), default="")
    work_branch: Mapped[str] = mapped_column(String(255), default="")
    worktree_path: Mapped[str] = mapped_column(Text, default="")
    current_run_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    claude_session_id: Mapped[str] = mapped_column(String(128), default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)

    tasks: Mapped[list[TaskRow]] = relationship(back_populates="job", cascade="all, delete-orphan")


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("job_id", "iteration", name="uq_tasks_job_iteration"),
        Index(
            "uq_tasks_job_command",
            "job_id",
            "command_key",
            unique=True,
            postgresql_where=text("command_key <> ''"),
            sqlite_where=text("command_key <> ''"),
        ),
    )

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
    command_key: Mapped[str] = mapped_column(String(255), default="")
    iteration: Mapped[int] = mapped_column(Integer, default=1)
    phase: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    verification: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    commit_sha: Mapped[str] = mapped_column(String(64), default="")
    remote_sha: Mapped[str] = mapped_column(String(64), default="")
    mr_url: Mapped[str] = mapped_column(Text, default="")
    ci_status: Mapped[str] = mapped_column(String(32), default="")
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


class TaskMessageRow(Base):
    """Durable Feishu inbox used to deduplicate event and message retries."""

    __tablename__ = "task_messages"
    __table_args__ = (
        Index(
            "uq_task_messages_tenant_event",
            "tenant_key",
            "event_id",
            unique=True,
            postgresql_where=text("event_id <> ''"),
            sqlite_where=text("event_id <> ''"),
        ),
        Index(
            "uq_task_messages_tenant_message",
            "tenant_key",
            "message_id",
            unique=True,
            postgresql_where=text("message_id <> ''"),
            sqlite_where=text("message_id <> ''"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    tenant_key: Mapped[str] = mapped_column(String(128), default="default")
    event_id: Mapped[str] = mapped_column(String(255), default="")
    message_id: Mapped[str] = mapped_column(String(255), default="")
    requester_id: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(32), default="message")
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="received", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskCommandRow(Base):
    """Maps a user command/card action to exactly one execution run."""

    __tablename__ = "task_commands"
    __table_args__ = (UniqueConstraint("task_id", "command_key", name="uq_task_commands_key"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    command_key: Mapped[str] = mapped_column(String(255))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)


class WorkerJobRow(Base):
    """Lease-backed durable queue row; one row exists for each run."""

    __tablename__ = "worker_jobs"
    __table_args__ = (UniqueConstraint("run_id", name="uq_worker_jobs_run"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    phase: Mapped[str] = mapped_column(String(32), default="queued")
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_token: Mapped[str] = mapped_column(String(128), default="", index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(128), default="")
    result: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    terminal: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)


class WorkerEventRow(Base):
    """Normalized, ordered Claude/worker event stream."""

    __tablename__ = "worker_events"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_no", "sequence", name="uq_worker_events_sequence"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(64), default="progress")
    phase: Mapped[str] = mapped_column(String(32), default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now_dt)


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
