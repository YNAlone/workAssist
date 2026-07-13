from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db import AuditLogRow, JobRow, TaskRow, session_scope, utc_now_dt
from .errors import JobNotFoundError, TaskNotFoundError
from .models import Job, JobStatus, PlatformTask, PlatformTaskStatus


def _dt_to_iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class PlatformStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sf = session_factory

    def save_job(self, job: Job) -> Job:
        with session_scope(self._sf) as session:
            row = session.get(JobRow, job.id)
            if row is None:
                row = JobRow(id=job.id)
                session.add(row)
            row.requester_id = job.requester_id
            row.chat_id = job.chat_id
            row.goal = job.goal
            row.status = job.status.value
            row.plan = job.plan
            row.updated_at = utc_now_dt()
            if row.created_at is None:
                row.created_at = utc_now_dt()
            session.flush()
        return job

    def get_job(self, job_id: str) -> Job | None:
        with session_scope(self._sf) as session:
            row = session.get(JobRow, job_id)
            if row is None:
                return None
            task_ids = [t.id for t in row.tasks]
            return Job(
                id=row.id,
                requester_id=row.requester_id,
                chat_id=row.chat_id,
                goal=row.goal,
                status=JobStatus(row.status),
                task_ids=task_ids,
                plan=row.plan or {},
                created_at=_dt_to_iso(row.created_at),
                updated_at=_dt_to_iso(row.updated_at),
            )

    def require_job(self, job_id: str) -> Job:
        job = self.get_job(job_id)
        if not job:
            raise JobNotFoundError(f"job not found: {job_id}")
        return job

    def save_task(self, task: PlatformTask) -> PlatformTask:
        with session_scope(self._sf) as session:
            row = session.get(TaskRow, task.id)
            if row is None:
                row = TaskRow(id=task.id, job_id=task.job_id)
                session.add(row)
            row.job_id = task.job_id
            row.agent_id = task.agent_id
            row.goal = task.goal
            row.inputs = task.inputs
            row.status = task.status.value
            row.result = task.result
            row.error = task.error
            row.chat_id = task.chat_id
            row.requester_id = task.requester_id
            row.updated_at = utc_now_dt()
            session.flush()
        return task

    def get_task(self, task_id: str) -> PlatformTask | None:
        with session_scope(self._sf) as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                return None
            return PlatformTask(
                id=row.id,
                job_id=row.job_id,
                agent_id=row.agent_id,
                goal=row.goal,
                inputs=row.inputs or {},
                status=PlatformTaskStatus(row.status),
                result=row.result or {},
                error=row.error or "",
                chat_id=row.chat_id,
                requester_id=row.requester_id,
                created_at=_dt_to_iso(row.created_at),
                updated_at=_dt_to_iso(row.updated_at),
            )

    def require_task(self, task_id: str) -> PlatformTask:
        task = self.get_task(task_id)
        if not task:
            raise TaskNotFoundError(f"task not found: {task_id}")
        return task

    def list_tasks_for_job(self, job_id: str) -> list[PlatformTask]:
        with session_scope(self._sf) as session:
            rows = list(session.scalars(select(TaskRow).where(TaskRow.job_id == job_id)))
            return [
                PlatformTask(
                    id=row.id,
                    job_id=row.job_id,
                    agent_id=row.agent_id,
                    goal=row.goal,
                    inputs=row.inputs or {},
                    status=PlatformTaskStatus(row.status),
                    result=row.result or {},
                    error=row.error or "",
                    chat_id=row.chat_id,
                    requester_id=row.requester_id,
                    created_at=_dt_to_iso(row.created_at),
                    updated_at=_dt_to_iso(row.updated_at),
                )
                for row in rows
            ]

    def append_audit(
        self,
        *,
        event: str,
        job_id: str = "",
        task_id: str = "",
        agent_id: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with session_scope(self._sf) as session:
            session.add(
                AuditLogRow(
                    job_id=job_id,
                    task_id=task_id,
                    agent_id=agent_id,
                    event=event,
                    payload=payload or {},
                )
            )
