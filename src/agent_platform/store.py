from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .db import (
    AuditLogRow,
    JobRow,
    TaskCommandRow,
    TaskMessageRow,
    TaskRow,
    session_scope,
    utc_now_dt,
)
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
            row.tenant_key = job.tenant_key
            row.requester_id = job.requester_id
            row.chat_id = job.chat_id
            row.goal = job.goal
            row.status = job.status.value
            row.plan = job.plan
            row.repo = job.repo
            row.base_branch = job.base_branch
            row.work_branch = job.work_branch
            row.worktree_path = job.worktree_path
            row.current_run_id = job.current_run_id
            row.claude_session_id = job.claude_session_id
            row.version = job.version
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
                tenant_key=row.tenant_key,
                repo=row.repo,
                base_branch=row.base_branch,
                work_branch=row.work_branch,
                worktree_path=row.worktree_path,
                current_run_id=row.current_run_id,
                claude_session_id=row.claude_session_id,
                version=row.version,
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
            row.command_key = task.command_key
            row.iteration = task.iteration
            row.phase = task.phase
            row.attempt_no = task.attempt_no
            row.verification = task.verification
            row.commit_sha = task.commit_sha
            row.remote_sha = task.remote_sha
            row.mr_url = task.mr_url
            row.ci_status = task.ci_status
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
                command_key=row.command_key,
                iteration=row.iteration,
                phase=row.phase,
                attempt_no=row.attempt_no,
                verification=row.verification or {},
                commit_sha=row.commit_sha,
                remote_sha=row.remote_sha,
                mr_url=row.mr_url,
                ci_status=row.ci_status,
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
                    command_key=row.command_key,
                    iteration=row.iteration,
                    phase=row.phase,
                    attempt_no=row.attempt_no,
                    verification=row.verification or {},
                    commit_sha=row.commit_sha,
                    remote_sha=row.remote_sha,
                    mr_url=row.mr_url,
                    ci_status=row.ci_status,
                    created_at=_dt_to_iso(row.created_at),
                    updated_at=_dt_to_iso(row.updated_at),
                )
                for row in rows
            ]

    def get_or_create_chat_task(
        self,
        *,
        tenant_key: str,
        chat_id: str,
        goal: str,
        requester_id: str = "",
        repo: str = "",
        base_branch: str = "",
        work_branch: str = "",
    ) -> tuple[Job, bool]:
        """Return the single durable business task for a tenant/chat pair."""
        tenant_key = tenant_key or "default"
        if not chat_id:
            raise ValueError("chat_id is required for chat task identity")
        session = self._sf()
        try:
            row = session.scalar(
                select(JobRow).where(JobRow.tenant_key == tenant_key, JobRow.chat_id == chat_id)
            )
            created = row is None
            if row is None:
                row = JobRow(
                    id=Job.create(goal=goal).id,
                    tenant_key=tenant_key,
                    chat_id=chat_id,
                    requester_id=requester_id,
                    goal=goal,
                    status=JobStatus.RECEIVED.value,
                    repo=repo,
                    base_branch=base_branch,
                    work_branch=work_branch,
                )
                session.add(row)
                session.flush()
            session.commit()
            return self._job_from_row(row), created
        except IntegrityError:
            session.rollback()
            row = session.scalar(
                select(JobRow).where(JobRow.tenant_key == tenant_key, JobRow.chat_id == chat_id)
            )
            if row is None:
                raise
            return self._job_from_row(row), False
        finally:
            session.close()

    def register_inbound_message(
        self,
        *,
        tenant_key: str,
        chat_id: str,
        event_id: str,
        message_id: str,
        requester_id: str,
        payload: dict[str, Any],
        kind: str = "message",
    ) -> dict[str, Any]:
        """Atomically bind and deduplicate a Feishu inbox item."""
        if not event_id and not message_id:
            raise ValueError("event_id or message_id is required")
        tenant_key = tenant_key or "default"
        session = self._sf()
        try:
            duplicate = self._find_message(session, tenant_key, event_id, message_id)
            if duplicate is not None:
                return self._message_result(
                    duplicate,
                    session.get(JobRow, duplicate.task_id),
                    duplicate=True,
                )

            task = session.scalar(
                select(JobRow)
                .where(JobRow.tenant_key == tenant_key, JobRow.chat_id == chat_id)
                .with_for_update()
            )
            if task is None:
                task = JobRow(
                    id=Job.create(goal="").id,
                    tenant_key=tenant_key,
                    chat_id=chat_id,
                    requester_id=requester_id,
                    status=JobStatus.RECEIVED.value,
                )
                session.add(task)
                session.flush()
            message = TaskMessageRow(
                task_id=task.id,
                tenant_key=tenant_key,
                event_id=event_id,
                message_id=message_id,
                requester_id=requester_id,
                kind=kind,
                payload=payload,
                status="received",
            )
            session.add(message)
            session.flush()
            session.commit()
            return self._message_result(message, task, duplicate=False)
        except IntegrityError:
            session.rollback()
            duplicate = self._find_message(session, tenant_key, event_id, message_id)
            if duplicate is None:
                raise
            return self._message_result(
                duplicate,
                session.get(JobRow, duplicate.task_id),
                duplicate=True,
            )
        finally:
            session.close()

    def complete_inbound_message(self, message_row_id: int, result: dict[str, Any]) -> None:
        """Persist the response used for deterministic Feishu retry replies."""
        with session_scope(self._sf) as session:
            row = session.get(TaskMessageRow, message_row_id)
            if row is None:
                return
            row.result = result
            row.status = "processed"
            row.processed_at = utc_now_dt()

    def claim_inbound_message(self, message_row_id: int) -> bool:
        """Claim inbox processing with a compare-and-set to stop concurrent delivery."""
        with session_scope(self._sf) as session:
            outcome = session.execute(
                update(TaskMessageRow)
                .where(
                    TaskMessageRow.id == message_row_id,
                    TaskMessageRow.status.in_(("received", "failed")),
                )
                .values(status="processing")
            )
            return bool(outcome.rowcount)

    def fail_inbound_message(self, message_row_id: int, error: str) -> None:
        """Release a failed inbox item so the next Feishu retry can process it."""
        with session_scope(self._sf) as session:
            row = session.get(TaskMessageRow, message_row_id)
            if row is None or row.status == "processed":
                return
            row.status = "failed"
            row.result = {"error": error}

    def create_run_idempotent(
        self,
        *,
        task_id: str,
        command_key: str,
        agent_id: str,
        goal: str,
        inputs: dict[str, Any],
        requester_id: str = "",
    ) -> tuple[PlatformTask, bool]:
        """Create exactly one run for a confirmation, retry, or card action."""
        if not command_key:
            raise ValueError("command_key is required")
        session = self._sf()
        try:
            command = session.scalar(
                select(TaskCommandRow).where(
                    TaskCommandRow.task_id == task_id,
                    TaskCommandRow.command_key == command_key,
                )
            )
            if command is not None:
                return self._task_from_row(session.get(TaskRow, command.run_id)), False

            task = session.scalar(
                select(JobRow).where(JobRow.id == task_id).with_for_update()
            )
            if task is None:
                raise JobNotFoundError(f"job not found: {task_id}")
            iteration = int(
                session.scalar(
                    select(func.coalesce(func.max(TaskRow.iteration), 0)).where(TaskRow.job_id == task_id)
                )
                or 0
            ) + 1
            run = TaskRow(
                id=Job.create(goal=goal).id,
                job_id=task_id,
                agent_id=agent_id,
                goal=goal,
                inputs=inputs,
                status=PlatformTaskStatus.QUEUED.value,
                phase="queued",
                command_key=command_key,
                iteration=iteration,
                chat_id=task.chat_id,
                requester_id=requester_id,
            )
            session.add(run)
            session.flush()
            session.add(
                TaskCommandRow(
                    task_id=task_id,
                    run_id=run.id,
                    command_key=command_key,
                    payload=inputs,
                )
            )
            task.current_run_id = run.id
            task.status = JobStatus.RUNNING.value
            task.updated_at = utc_now_dt()
            session.commit()
            return self._task_from_row(run), True
        except IntegrityError:
            session.rollback()
            command = session.scalar(
                select(TaskCommandRow).where(
                    TaskCommandRow.task_id == task_id,
                    TaskCommandRow.command_key == command_key,
                )
            )
            if command is None:
                raise
            return self._task_from_row(session.get(TaskRow, command.run_id)), False
        finally:
            session.close()

    @staticmethod
    def _find_message(
        session: Session,
        tenant_key: str,
        event_id: str,
        message_id: str,
    ) -> TaskMessageRow | None:
        predicates = []
        if event_id:
            predicates.append(TaskMessageRow.event_id == event_id)
        if message_id:
            predicates.append(TaskMessageRow.message_id == message_id)
        return session.scalar(
            select(TaskMessageRow).where(
                TaskMessageRow.tenant_key == tenant_key,
                or_(*predicates),
            )
        )

    @staticmethod
    def _message_result(
        message: TaskMessageRow,
        task: JobRow | None,
        *,
        duplicate: bool,
    ) -> dict[str, Any]:
        return {
            "message_row_id": message.id,
            "task_id": message.task_id,
            "status": message.status,
            "result": message.result or {},
            "duplicate": duplicate,
            "task_status": task.status if task is not None else "",
        }

    @staticmethod
    def _job_from_row(row: JobRow) -> Job:
        return Job(
            id=row.id,
            requester_id=row.requester_id,
            chat_id=row.chat_id,
            goal=row.goal,
            status=JobStatus(row.status),
            task_ids=[task.id for task in row.tasks],
            plan=row.plan or {},
            tenant_key=row.tenant_key,
            repo=row.repo,
            base_branch=row.base_branch,
            work_branch=row.work_branch,
            worktree_path=row.worktree_path,
            current_run_id=row.current_run_id,
            claude_session_id=row.claude_session_id,
            version=row.version,
            created_at=_dt_to_iso(row.created_at),
            updated_at=_dt_to_iso(row.updated_at),
        )

    @staticmethod
    def _task_from_row(row: TaskRow | None) -> PlatformTask:
        if row is None:
            raise TaskNotFoundError("run not found")
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
            command_key=row.command_key,
            iteration=row.iteration,
            phase=row.phase,
            attempt_no=row.attempt_no,
            verification=row.verification or {},
            commit_sha=row.commit_sha,
            remote_sha=row.remote_sha,
            mr_url=row.mr_url,
            ci_status=row.ci_status,
            created_at=_dt_to_iso(row.created_at),
            updated_at=_dt_to_iso(row.updated_at),
        )

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
