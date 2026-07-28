from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .db import JobRow, TaskRow, WorkerEventRow, WorkerJobRow, session_scope, utc_now_dt
from .errors import StaleLeaseError, WorkerJobNotFoundError

TERMINAL_WORKER_STATUSES = {"completed", "succeeded", "failed", "cancelled", "needs_attention"}
SUCCESS_WORKER_STATUSES = {"completed", "succeeded"}


def _iso(value: datetime | None) -> str:
    """Serialize database timestamps consistently for the worker protocol."""
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


class DurableWorkerQueue:
    """PostgreSQL-backed worker queue with leases, fencing, and event deduplication."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int = 45,
        max_recoveries: int = 1,
    ) -> None:
        self._sf = session_factory
        self.lease_seconds = max(15, lease_seconds)
        self.max_recoveries = max(0, max_recoveries)

    def enqueue(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create one queue row per run and return the existing row on retries."""
        run_id = str(payload.get("run_id") or payload.get("job_id") or "").strip()
        requested_task_id = str(payload.get("task_id") or payload.get("session_id") or run_id).strip()
        if not run_id:
            raise ValueError("run_id is required")
        if not requested_task_id:
            raise ValueError("task_id is required")

        with session_scope(self._sf) as session:
            task_row = self._ensure_task_and_run(session, requested_task_id, run_id, payload)
            task = session.get(JobRow, task_row.job_id)
            effective_payload = dict(payload)
            if task is not None:
                # The chat task owns repository/worktree identity across all later runs.
                for key, value in (
                    ("repo", task.repo),
                    ("base_branch", task.base_branch),
                    ("work_branch", task.work_branch),
                    ("worktree_path", task.worktree_path),
                ):
                    if value:
                        effective_payload[key] = value
            existing = session.scalar(select(WorkerJobRow).where(WorkerJobRow.run_id == run_id))
            if existing is None:
                existing = WorkerJobRow(
                    run_id=run_id,
                    task_id=task_row.job_id,
                    payload={
                        **effective_payload,
                        "task_id": task_row.job_id,
                        "run_id": run_id,
                        "job_id": run_id,
                    },
                    status="queued",
                    phase="queued",
                )
                session.add(existing)
                session.flush()
            return self._serialize(existing)

    def _ensure_task_and_run(
        self,
        session: Session,
        task_id: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> TaskRow:
        """Create compatibility identity rows when a legacy dispatch reaches the durable queue."""
        tenant_key = str(payload.get("tenant_key") or "default")
        chat_id = str(payload.get("chat_id") or "")
        job = None
        if chat_id:
            job = session.scalar(
                select(JobRow).where(JobRow.tenant_key == tenant_key, JobRow.chat_id == chat_id)
            )
        if job is None:
            job = session.get(JobRow, task_id)
        if job is None:
            job = JobRow(
                id=task_id,
                tenant_key=tenant_key,
                requester_id=str(payload.get("requester_id") or ""),
                chat_id=chat_id,
                goal=str(payload.get("prompt") or ""),
                status="running",
                repo=str(payload.get("repo") or ""),
                base_branch=str(payload.get("base_branch") or ""),
                work_branch=str(payload.get("work_branch") or ""),
            )
            session.add(job)
            session.flush()
        else:
            job.goal = job.goal or str(payload.get("prompt") or "")
            job.requester_id = job.requester_id or str(payload.get("requester_id") or "")
            job.repo = job.repo or str(payload.get("repo") or "")
            job.base_branch = job.base_branch or str(payload.get("base_branch") or "")
            job.work_branch = job.work_branch or str(payload.get("work_branch") or "")

        run = session.get(TaskRow, run_id)
        if run is None:
            current_iteration = session.scalar(
                select(func.coalesce(func.max(TaskRow.iteration), 0)).where(TaskRow.job_id == job.id)
            )
            run = TaskRow(
                id=run_id,
                job_id=job.id,
                agent_id="code",
                goal=str(payload.get("prompt") or ""),
                inputs=dict(payload),
                status="queued",
                phase="queued",
                iteration=int(current_iteration or 0) + 1,
                chat_id=chat_id,
                requester_id=str(payload.get("requester_id") or ""),
            )
            session.add(run)
            session.flush()
        job.current_run_id = run.id
        job.status = "running"
        job.updated_at = utc_now_dt()
        return run

    def claim(self, *, worker_id: str = "") -> dict[str, Any] | None:
        """Atomically lease the oldest queued or safely recoverable expired job."""
        now = utc_now_dt()
        with session_scope(self._sf) as session:
            exhausted = list(
                session.scalars(
                    select(WorkerJobRow)
                    .where(
                        WorkerJobRow.terminal.is_(False),
                        WorkerJobRow.status == "leased",
                        WorkerJobRow.lease_expires_at < now,
                        WorkerJobRow.recovery_count >= self.max_recoveries,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for stale in exhausted:
                stale.status = "needs_attention"
                stale.phase = "needs_attention"
                stale.error = "worker lease expired after the allowed recovery"
                stale.terminal = True
                stale.updated_at = now
                run = session.get(TaskRow, stale.run_id)
                task = session.get(JobRow, stale.task_id)
                if run is not None:
                    run.status = "needs_attention"
                    run.phase = "needs_attention"
                    run.error = stale.error
                    run.updated_at = now
                if task is not None:
                    task.status = "needs_attention"
                    task.updated_at = now
            query = (
                select(WorkerJobRow)
                .where(
                    WorkerJobRow.terminal.is_(False),
                    or_(
                        WorkerJobRow.status == "queued",
                        (
                            (WorkerJobRow.status == "leased")
                            & (WorkerJobRow.lease_expires_at < now)
                            & (WorkerJobRow.recovery_count < self.max_recoveries)
                        ),
                    ),
                )
                .order_by(WorkerJobRow.created_at, WorkerJobRow.id)
                .with_for_update(skip_locked=True)
            )
            row = session.scalar(query)
            if row is None:
                return None

            expired_recovery = row.status == "leased"
            recovering = expired_recovery or row.recovery_count > 0
            if expired_recovery:
                row.recovery_count += 1
            row.attempt_no += 1
            row.status = "leased"
            row.phase = "leased"
            row.lease_token = secrets.token_urlsafe(32)
            row.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            row.heartbeat_at = now
            row.worker_id = worker_id
            row.updated_at = now

            run = session.get(TaskRow, row.run_id)
            if run is not None:
                run.status = "running"
                run.phase = "leased"
                run.attempt_no = row.attempt_no
                run.updated_at = now
            result = self._serialize(row)
            result["recovery"] = recovering
            return result

    def heartbeat(
        self,
        run_id: str,
        *,
        attempt_no: int,
        lease_token: str,
        phase: str = "",
    ) -> dict[str, Any]:
        """Renew a lease only when all fencing coordinates still match."""
        now = utc_now_dt()
        with session_scope(self._sf) as session:
            row = self._require_current_lease(session, run_id, attempt_no, lease_token)
            row.heartbeat_at = now
            row.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            row.updated_at = now
            if phase:
                row.phase = phase
                run = session.get(TaskRow, row.run_id)
                if run is not None:
                    run.phase = phase
                    run.updated_at = now
            return self._serialize(row)

    def append_event(
        self,
        run_id: str,
        *,
        attempt_no: int,
        lease_token: str,
        sequence: int,
        event_type: str,
        phase: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist an ordered event, treating a repeated sequence as a successful replay."""
        with session_scope(self._sf) as session:
            row = self._require_current_lease(session, run_id, attempt_no, lease_token)
            existing = session.scalar(
                select(WorkerEventRow).where(
                    WorkerEventRow.run_id == run_id,
                    WorkerEventRow.attempt_no == attempt_no,
                    WorkerEventRow.sequence == sequence,
                )
            )
            duplicate = existing is not None
            if existing is None:
                existing = WorkerEventRow(
                    run_id=run_id,
                    task_id=row.task_id,
                    attempt_no=attempt_no,
                    sequence=sequence,
                    event_type=event_type,
                    phase=phase,
                    payload=payload,
                )
                session.add(existing)
                try:
                    session.flush()
                except IntegrityError:
                    # A concurrent duplicate has already committed the same event key.
                    session.rollback()
                    return {
                        "run_id": run_id,
                        "attempt_no": attempt_no,
                        "sequence": sequence,
                        "duplicate": True,
                    }
            if phase:
                row.phase = phase
            durable_fields = {
                key: payload[key]
                for key in (
                    "claude_session_id",
                    "worktree_path",
                    "commit_sha",
                    "remote_sha",
                    "pr_url",
                    "verification",
                )
                if payload.get(key)
            }
            if durable_fields:
                row.result = {**(row.result or {}), **durable_fields}
                task = session.get(JobRow, row.task_id)
                run = session.get(TaskRow, row.run_id)
                if task is not None:
                    task.claude_session_id = str(
                        durable_fields.get("claude_session_id") or task.claude_session_id
                    )
                    task.worktree_path = str(
                        durable_fields.get("worktree_path") or task.worktree_path
                    )
                if run is not None:
                    run.commit_sha = str(durable_fields.get("commit_sha") or run.commit_sha)
                    run.remote_sha = str(durable_fields.get("remote_sha") or run.remote_sha)
                    run.mr_url = str(durable_fields.get("pr_url") or run.mr_url)
                    run.verification = durable_fields.get("verification") or run.verification
            return {
                "run_id": run_id,
                "attempt_no": attempt_no,
                "sequence": sequence,
                "duplicate": duplicate,
            }

    def complete(
        self,
        run_id: str,
        *,
        attempt_no: int,
        lease_token: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        """Finish the current attempt once; duplicate terminal calls return the stored result."""
        normalized = status.lower().strip() or "completed"
        now = utc_now_dt()
        with session_scope(self._sf) as session:
            row = session.scalar(select(WorkerJobRow).where(WorkerJobRow.run_id == run_id))
            if row is None:
                raise WorkerJobNotFoundError(f"worker job not found: {run_id}")
            if row.terminal:
                if row.attempt_no == attempt_no and row.lease_token == lease_token:
                    return self._serialize(row)
                raise StaleLeaseError(f"stale terminal callback for run {run_id}")
            self._assert_lease(row, attempt_no, lease_token)

            retrying = normalized == "awaiting_retry" and row.recovery_count < self.max_recoveries
            if retrying:
                row.recovery_count += 1
                row.status = "queued"
                row.phase = "awaiting_retry"
                row.lease_expires_at = None
            else:
                if normalized == "awaiting_retry":
                    normalized = "needs_attention"
                row.status = normalized
                row.phase = "succeeded" if normalized in SUCCESS_WORKER_STATUSES else normalized
            row.result = {**(row.result or {}), **(result or {})}
            row.error = error
            row.terminal = normalized in TERMINAL_WORKER_STATUSES
            row.updated_at = now

            run = session.get(TaskRow, row.run_id)
            if run is not None:
                run.phase = row.phase
                run.status = (
                    "queued"
                    if retrying
                    else "succeeded" if normalized in SUCCESS_WORKER_STATUSES else normalized
                )
                run.result = {**(run.result or {}), **(result or {})}
                run.error = error
                run.commit_sha = str((result or {}).get("commit_sha") or run.commit_sha)
                run.remote_sha = str((result or {}).get("remote_sha") or run.remote_sha)
                run.mr_url = str((result or {}).get("pr_url") or run.mr_url)
                run.verification = (result or {}).get("verification") or run.verification
                run.updated_at = now

            task = session.get(JobRow, row.task_id)
            if task is not None:
                if retrying:
                    task.status = "awaiting_retry"
                elif normalized in SUCCESS_WORKER_STATUSES:
                    task.status = "done"
                elif normalized == "cancelled":
                    task.status = "cancelled"
                else:
                    task.status = "needs_attention"
                task.claude_session_id = str(
                    (result or {}).get("claude_session_id") or task.claude_session_id
                )
                task.worktree_path = str((result or {}).get("worktree_path") or task.worktree_path)
                task.version += 1
                task.updated_at = now
            return self._serialize(row)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with session_scope(self._sf) as session:
            row = session.scalar(select(WorkerJobRow).where(WorkerJobRow.run_id == run_id))
            return self._serialize(row) if row is not None else None

    def cancel(self, run_id: str, *, reason: str = "cancelled by user") -> dict[str, Any] | None:
        """Cancel a run and invalidate its fencing token so the worker stops safely."""
        now = utc_now_dt()
        with session_scope(self._sf) as session:
            row = session.scalar(
                select(WorkerJobRow).where(WorkerJobRow.run_id == run_id).with_for_update()
            )
            if row is None:
                return None
            if row.terminal:
                return self._serialize(row)
            row.status = "cancelled"
            row.phase = "cancelled"
            row.error = reason
            row.terminal = True
            row.lease_token = ""
            row.lease_expires_at = now
            row.updated_at = now
            run = session.get(TaskRow, row.run_id)
            task = session.get(JobRow, row.task_id)
            if run is not None:
                run.status = "cancelled"
                run.phase = "cancelled"
                run.error = reason
                run.updated_at = now
            if task is not None:
                task.status = "cancelled"
                task.updated_at = now
            return self._serialize(row)

    def _require_current_lease(
        self,
        session: Session,
        run_id: str,
        attempt_no: int,
        lease_token: str,
    ) -> WorkerJobRow:
        row = session.scalar(
            select(WorkerJobRow).where(WorkerJobRow.run_id == run_id).with_for_update()
        )
        if row is None:
            raise WorkerJobNotFoundError(f"worker job not found: {run_id}")
        if row.terminal:
            raise StaleLeaseError(f"run is already terminal: {run_id}")
        self._assert_lease(row, attempt_no, lease_token)
        return row

    @staticmethod
    def _assert_lease(row: WorkerJobRow, attempt_no: int, lease_token: str) -> None:
        if row.attempt_no != attempt_no or not lease_token or row.lease_token != lease_token:
            raise StaleLeaseError(f"lease no longer owned for run {row.run_id}")
        expires = row.lease_expires_at
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= utc_now_dt():
                raise StaleLeaseError(f"lease expired for run {row.run_id}")

    @staticmethod
    def _serialize(row: WorkerJobRow) -> dict[str, Any]:
        data = dict(row.payload or {})
        data.update(
            {
                "id": row.id,
                "task_id": row.task_id,
                "run_id": row.run_id,
                # job_id is retained as a one-version compatibility alias for run_id.
                "job_id": row.run_id,
                "status": row.status,
                "phase": row.phase,
                "attempt_no": row.attempt_no,
                "recovery_count": row.recovery_count,
                "lease_token": row.lease_token,
                "lease_expires_at": _iso(row.lease_expires_at),
                "heartbeat_at": _iso(row.heartbeat_at),
                "worker_id": row.worker_id,
                "result": row.result or {},
                "error": row.error,
                "terminal": row.terminal,
            }
        )
        return data
