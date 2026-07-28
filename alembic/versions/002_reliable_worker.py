"""add durable task identity and lease-backed worker tables

Revision ID: 002_reliable_worker
Revises: 001_initial
Create Date: 2026-07-27
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_reliable_worker"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JsonType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    # Jobs are the stable chat-scoped business tasks.
    op.add_column("jobs", sa.Column("tenant_key", sa.String(128), nullable=False, server_default="default"))
    op.add_column("jobs", sa.Column("repo", sa.String(512), nullable=False, server_default=""))
    op.add_column("jobs", sa.Column("base_branch", sa.String(255), nullable=False, server_default=""))
    op.add_column("jobs", sa.Column("work_branch", sa.String(255), nullable=False, server_default=""))
    op.add_column("jobs", sa.Column("worktree_path", sa.Text(), nullable=False, server_default=""))
    op.add_column("jobs", sa.Column("current_run_id", sa.String(32), nullable=False, server_default=""))
    op.add_column("jobs", sa.Column("claude_session_id", sa.String(128), nullable=False, server_default=""))
    op.add_column("jobs", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.create_index("ix_jobs_tenant_key", "jobs", ["tenant_key"])
    op.create_index("ix_jobs_current_run_id", "jobs", ["current_run_id"])
    op.create_index(
        "uq_jobs_tenant_chat",
        "jobs",
        ["tenant_key", "chat_id"],
        unique=True,
        postgresql_where=sa.text("chat_id <> ''"),
    )

    # Tasks are individual execution runs.
    op.add_column("tasks", sa.Column("command_key", sa.String(255), nullable=False, server_default=""))
    op.add_column("tasks", sa.Column("iteration", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("tasks", sa.Column("phase", sa.String(32), nullable=False, server_default="queued"))
    op.add_column("tasks", sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("tasks", sa.Column("verification", JsonType, nullable=False, server_default="{}"))
    op.add_column("tasks", sa.Column("commit_sha", sa.String(64), nullable=False, server_default=""))
    op.add_column("tasks", sa.Column("remote_sha", sa.String(64), nullable=False, server_default=""))
    op.add_column("tasks", sa.Column("mr_url", sa.Text(), nullable=False, server_default=""))
    op.add_column("tasks", sa.Column("ci_status", sa.String(32), nullable=False, server_default=""))
    op.create_index("ix_tasks_phase", "tasks", ["phase"])
    op.execute(
        """
        WITH numbered AS (
          SELECT id, ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY created_at, id) AS n
          FROM tasks
        )
        UPDATE tasks SET iteration = numbered.n FROM numbered WHERE tasks.id = numbered.id
        """
    )
    op.create_unique_constraint("uq_tasks_job_iteration", "tasks", ["job_id", "iteration"])
    op.create_index(
        "uq_tasks_job_command",
        "tasks",
        ["job_id", "command_key"],
        unique=True,
        postgresql_where=sa.text("command_key <> ''"),
    )

    op.create_table(
        "task_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tenant_key", sa.String(128), nullable=False, server_default="default"),
        sa.Column("event_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("message_id", sa.String(255), nullable=False, server_default=""),
        sa.Column("requester_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("kind", sa.String(32), nullable=False, server_default="message"),
        sa.Column("payload", JsonType, nullable=False, server_default="{}"),
        sa.Column("result", JsonType, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(32), nullable=False, server_default="received"),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_task_messages_task_id", "task_messages", ["task_id"])
    op.create_index("ix_task_messages_status", "task_messages", ["status"])
    op.create_index(
        "uq_task_messages_tenant_event",
        "task_messages",
        ["tenant_key", "event_id"],
        unique=True,
        postgresql_where=sa.text("event_id <> ''"),
    )
    op.create_index(
        "uq_task_messages_tenant_message",
        "task_messages",
        ["tenant_key", "message_id"],
        unique=True,
        postgresql_where=sa.text("message_id <> ''"),
    )

    op.create_table(
        "task_commands",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("command_key", sa.String(255), nullable=False),
        sa.Column("payload", JsonType, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("task_id", "command_key", name="uq_task_commands_key"),
    )
    op.create_index("ix_task_commands_task_id", "task_commands", ["task_id"])
    op.create_index("ix_task_commands_run_id", "task_commands", ["run_id"])

    op.create_table(
        "worker_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("payload", JsonType, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("phase", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(128), nullable=False, server_default=""),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("result", JsonType, nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("terminal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", name="uq_worker_jobs_run"),
    )
    op.create_index("ix_worker_jobs_run_id", "worker_jobs", ["run_id"])
    op.create_index("ix_worker_jobs_task_id", "worker_jobs", ["task_id"])
    op.create_index("ix_worker_jobs_status", "worker_jobs", ["status"])
    op.create_index("ix_worker_jobs_lease_token", "worker_jobs", ["lease_token"])
    op.create_index("ix_worker_jobs_lease_expires_at", "worker_jobs", ["lease_expires_at"])

    op.create_table(
        "worker_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", sa.String(32), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False, server_default="progress"),
        sa.Column("phase", sa.String(32), nullable=False, server_default=""),
        sa.Column("payload", JsonType, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "attempt_no", "sequence", name="uq_worker_events_sequence"),
    )
    op.create_index("ix_worker_events_run_id", "worker_events", ["run_id"])
    op.create_index("ix_worker_events_task_id", "worker_events", ["task_id"])

    # Old claimed file jobs cannot prove ownership and require explicit recovery.
    op.execute("UPDATE tasks SET phase = 'needs_attention' WHERE status = 'claimed'")


def downgrade() -> None:
    op.drop_table("worker_events")
    op.drop_table("worker_jobs")
    op.drop_table("task_commands")
    op.drop_table("task_messages")
    op.drop_index("uq_tasks_job_command", table_name="tasks")
    op.drop_constraint("uq_tasks_job_iteration", "tasks", type_="unique")
    for column in (
        "ci_status",
        "mr_url",
        "remote_sha",
        "commit_sha",
        "verification",
        "attempt_no",
        "phase",
        "iteration",
        "command_key",
    ):
        op.drop_column("tasks", column)
    op.drop_index("uq_jobs_tenant_chat", table_name="jobs")
    for column in (
        "version",
        "claude_session_id",
        "current_run_id",
        "worktree_path",
        "work_branch",
        "base_branch",
        "repo",
        "tenant_key",
    ):
        op.drop_column("jobs", column)
