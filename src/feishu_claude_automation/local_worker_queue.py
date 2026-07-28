from __future__ import annotations

import json
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, TextIO

from .models import utc_now

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def _lock_file(handle: TextIO) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_file(handle: TextIO) -> None:
    if sys.platform == "win32":
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class LocalWorkerQueue:
    """Migration/test queue implementing the same lease protocol as PostgreSQL."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    @contextmanager
    def _with_lock(self, mutate: bool = False) -> Iterator[list[dict[str, Any]]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("r+" if self.path.exists() else "w+")
        _lock_file(handle)
        try:
            handle.seek(0)
            raw = handle.read()
            jobs = json.loads(raw) if raw.strip() else []
            if not isinstance(jobs, list):
                jobs = []
            yield jobs
            if mutate:
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(jobs, ensure_ascii=False, indent=2))
                handle.flush()
        finally:
            _unlock_file(handle)
            handle.close()

    def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        run_id = str(job.get("run_id") or job.get("job_id") or "")
        task_id = str(job.get("task_id") or run_id)
        job = {
            **job,
            "task_id": task_id,
            "run_id": run_id,
            "job_id": run_id,
            "status": "queued",
            "phase": "queued",
            "attempt_no": 0,
            "recovery_count": 0,
            "terminal": False,
            "queued_at": utc_now(),
        }
        with self._with_lock(mutate=True) as jobs:
            for existing in jobs:
                if str(existing.get("run_id") or existing.get("job_id")) == run_id:
                    return dict(existing)
            jobs.append(job)
        return job

    def claim(self, *, worker_id: str = "") -> dict[str, Any] | None:
        with self._with_lock(mutate=True) as jobs:
            for item in jobs:
                expired = self._lease_expired(item)
                expired_recovery = (
                    item.get("status") == "leased"
                    and expired
                    and int(item.get("recovery_count") or 0) < 1
                )
                if item.get("status") == "queued" or expired_recovery:
                    if expired_recovery:
                        item["recovery_count"] = int(item.get("recovery_count") or 0) + 1
                    item["status"] = "leased"
                    item["phase"] = "leased"
                    item["attempt_no"] = int(item.get("attempt_no") or 0) + 1
                    item["lease_token"] = secrets.token_urlsafe(32)
                    item["lease_expires_at"] = (
                        datetime.now(timezone.utc) + timedelta(seconds=45)
                    ).isoformat()
                    item["heartbeat_at"] = utc_now()
                    item["worker_id"] = worker_id
                    item["claimed_at"] = utc_now()
                    item["recovery"] = (
                        expired_recovery or int(item.get("recovery_count") or 0) > 0
                    )
                    return dict(item)
        return None

    def heartbeat(
        self,
        run_id: str,
        *,
        attempt_no: int,
        lease_token: str,
        phase: str = "",
    ) -> dict[str, Any]:
        with self._with_lock(mutate=True) as jobs:
            item = self._require_lease(jobs, run_id, attempt_no, lease_token)
            item["heartbeat_at"] = utc_now()
            item["lease_expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=45)
            ).isoformat()
            if phase:
                item["phase"] = phase
            return dict(item)

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
        with self._with_lock(mutate=True) as jobs:
            item = self._require_lease(jobs, run_id, attempt_no, lease_token)
            events = item.setdefault("events", [])
            if any(
                int(event.get("attempt_no") or 0) == attempt_no
                and int(event.get("sequence") or 0) == sequence
                for event in events
            ):
                return {"run_id": run_id, "sequence": sequence, "duplicate": True}
            events.append(
                {
                    "attempt_no": attempt_no,
                    "sequence": sequence,
                    "event_type": event_type,
                    "phase": phase,
                    "payload": payload,
                }
            )
            item["phase"] = phase or item.get("phase", "")
            return {"run_id": run_id, "sequence": sequence, "duplicate": False}

    def complete(
        self,
        run_id: str,
        *,
        attempt_no: int = 0,
        lease_token: str = "",
        status: str = "completed",
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any] | None:
        with self._with_lock(mutate=True) as jobs:
            for item in jobs:
                if str(item.get("run_id") or item.get("job_id")) == run_id:
                    if attempt_no or lease_token:
                        self._assert_lease(item, attempt_no, lease_token)
                    retrying = (
                        status == "awaiting_retry"
                        and int(item.get("recovery_count") or 0) < 1
                    )
                    if retrying:
                        item["recovery_count"] = int(item.get("recovery_count") or 0) + 1
                        item["status"] = "queued"
                        item["phase"] = "awaiting_retry"
                        item["terminal"] = False
                    else:
                        if status == "awaiting_retry":
                            status = "needs_attention"
                        item["status"] = status
                        item["phase"] = (
                            "succeeded" if status in {"completed", "succeeded"} else status
                        )
                        item["terminal"] = True
                    item["result"] = result or {}
                    item["error"] = error
                    item["completed_at"] = utc_now()
                    return dict(item)
        return None

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._with_lock(mutate=False) as jobs:
            return [dict(item) for item in jobs]

    def cancel(self, run_id: str, *, reason: str = "cancelled by user") -> dict[str, Any] | None:
        """Cancel a compatibility queue row and invalidate its lease."""
        with self._with_lock(mutate=True) as jobs:
            for item in jobs:
                if str(item.get("run_id") or item.get("job_id")) != run_id:
                    continue
                if item.get("terminal"):
                    return dict(item)
                item["status"] = "cancelled"
                item["phase"] = "cancelled"
                item["terminal"] = True
                item["lease_token"] = ""
                item["error"] = reason
                item["completed_at"] = utc_now()
                return dict(item)
        return None

    @staticmethod
    def _lease_expired(item: dict[str, Any]) -> bool:
        raw = str(item.get("lease_expires_at") or "")
        if not raw:
            return True
        try:
            expires = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= datetime.now(timezone.utc)

    @classmethod
    def _require_lease(
        cls,
        jobs: list[dict[str, Any]],
        run_id: str,
        attempt_no: int,
        lease_token: str,
    ) -> dict[str, Any]:
        for item in jobs:
            if str(item.get("run_id") or item.get("job_id")) == run_id:
                cls._assert_lease(item, attempt_no, lease_token)
                return item
        raise RuntimeError(f"worker job not found: {run_id}")

    @classmethod
    def _assert_lease(cls, item: dict[str, Any], attempt_no: int, lease_token: str) -> None:
        if (
            int(item.get("attempt_no") or 0) != attempt_no
            or not lease_token
            or str(item.get("lease_token") or "") != lease_token
            or cls._lease_expired(item)
        ):
            raise RuntimeError(
                f"stale or expired lease for run {item.get('run_id') or item.get('job_id')}"
            )
