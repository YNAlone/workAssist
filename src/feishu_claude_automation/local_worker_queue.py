from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .models import utc_now


class LocalWorkerQueue:
    """File-backed queue for local worker jobs with POSIX flock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    @contextmanager
    def _with_lock(self, mutate: bool = False) -> Iterator[list[dict[str, Any]]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("r+" if self.path.exists() else "w+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        job = {**job, "status": "queued", "queued_at": utc_now()}
        with self._with_lock(mutate=True) as jobs:
            jobs.append(job)
        return job

    def claim(self) -> dict[str, Any] | None:
        with self._with_lock(mutate=True) as jobs:
            for item in jobs:
                if item.get("status") == "queued":
                    item["status"] = "claimed"
                    item["claimed_at"] = utc_now()
                    return dict(item)
        return None

    def complete(self, job_id: str, *, status: str = "completed") -> None:
        with self._with_lock(mutate=True) as jobs:
            for item in jobs:
                if item.get("job_id") == job_id:
                    item["status"] = status
                    item["completed_at"] = utc_now()
                    return

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._with_lock(mutate=False) as jobs:
            return [dict(item) for item in jobs]
