"""initial agent_platform tables

Revision ID: 001_initial
Revises:
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JsonType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("requester_id", sa.String(length=128), server_default=""),
        sa.Column("chat_id", sa.String(length=128), server_default=""),
        sa.Column("goal", sa.Text(), server_default=""),
        sa.Column("status", sa.String(length=32), server_default="received"),
        sa.Column("plan", JsonType, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_jobs_requester_id", "jobs", ["requester_id"])
    op.create_index("ix_jobs_chat_id", "jobs", ["chat_id"])
    op.create_index("ix_jobs_status", "jobs", ["status"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("job_id", sa.String(length=32), sa.ForeignKey("jobs.id", ondelete="CASCADE")),
        sa.Column("agent_id", sa.String(length=64)),
        sa.Column("goal", sa.Text(), server_default=""),
        sa.Column("inputs", JsonType, server_default="{}"),
        sa.Column("status", sa.String(length=32), server_default="queued"),
        sa.Column("result", JsonType, server_default="{}"),
        sa.Column("error", sa.Text(), server_default=""),
        sa.Column("chat_id", sa.String(length=128), server_default=""),
        sa.Column("requester_id", sa.String(length=128), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_tasks_job_id", "tasks", ["job_id"])
    op.create_index("ix_tasks_agent_id", "tasks", ["agent_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(length=32), server_default=""),
        sa.Column("task_id", sa.String(length=32), server_default=""),
        sa.Column("agent_id", sa.String(length=64), server_default=""),
        sa.Column("event", sa.String(length=128), server_default=""),
        sa.Column("payload", JsonType, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_audit_logs_job_id", "audit_logs", ["job_id"])
    op.create_index("ix_audit_logs_task_id", "audit_logs", ["task_id"])

    op.create_table(
        "user_oauth",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("requester_id", sa.String(length=128), unique=True),
        sa.Column("agent_id", sa.String(length=64), server_default="doc"),
        sa.Column("access_token_enc", sa.Text(), server_default=""),
        sa.Column("refresh_token_enc", sa.Text(), server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", sa.Text(), server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_user_oauth_requester_id", "user_oauth", ["requester_id"], unique=True)


def downgrade() -> None:
    op.drop_table("user_oauth")
    op.drop_table("audit_logs")
    op.drop_table("tasks")
    op.drop_table("jobs")
