from __future__ import annotations

import json
import urllib.error
import urllib.request
import socket
from typing import Any

from agent_platform.db import create_db_engine, create_session_factory, init_db
from agent_platform.worker_store import DurableWorkerQueue

from .config import Settings
from .github import GitHubClient
from .local_worker_queue import LocalWorkerQueue
from .models import Task
from .policy import Policy


class LocalWorkerClient:
    """Enqueue tasks for the on-machine worker process.

    When ``LOCAL_WORKER_ORCHESTRATOR_URL`` is set, claim/complete go through
    the remote Orchestrator HTTP API so the worker can run on a different
    machine from the Orchestrator.
    """

    def __init__(self, settings: Settings, policy: Policy) -> None:
        self.settings = settings
        self.policy = policy
        if settings.database_url:
            engine = create_db_engine(settings.database_url)
            init_db(engine)
            self.queue: DurableWorkerQueue | LocalWorkerQueue = DurableWorkerQueue(
                create_session_factory(engine),
                lease_seconds=settings.local_worker_lease_seconds,
                max_recoveries=settings.local_worker_max_recoveries,
            )
        else:
            # Compatibility is intentionally limited to explicit test/migration configurations.
            self.queue = LocalWorkerQueue(settings.local_worker_queue_path)
        self.worker_id = settings.local_worker_id or socket.gethostname()

    @property
    def remote_mode(self) -> bool:
        return bool(self.settings.local_worker_orchestrator_url)

    def enqueue(self, task: Task, *, local_path: str, provider: str) -> dict[str, Any]:
        if not self.settings.local_worker_enabled and not self.settings.dry_run:
            raise RuntimeError("LOCAL_WORKER_ENABLED is false")

        prompt = GitHubClient._wrap_prompt(task)
        job = {
            "task_id": task.session_id or task.parent_task_id or task.id,
            "run_id": task.id,
            "job_id": task.id,
            "repo": task.repo,
            "prompt": prompt,
            "base_branch": task.base_branch,
            "work_branch": task.work_branch,
            "mode": task.mode.value,
            "delivery": task.delivery or "push",
            "analysis_only": task.analysis_only,
            "provider": provider,
            "local_path": local_path,
            "model": task.model or self.settings.anthropic_model,
            "requester_id": task.requester_id,
            "chat_id": task.chat_id,
            "verify_commands": self.policy.verify_commands_for(task.repo),
            "callback_url": f"{self.settings.callback_base_url.rstrip('/')}/callbacks/runner",
        }
        if self.settings.dry_run:
            return {"dry_run": True, "executor": "local_worker", **job}
        saved = self.queue.enqueue(job)
        # Persist the aggregate-owned branch back into the legacy compatibility task.
        task.work_branch = str(saved.get("work_branch") or task.work_branch)
        return {
            "executor": "local_worker",
            "task_id": saved.get("task_id") or job["task_id"],
            "run_id": saved.get("run_id") or task.id,
            "job_id": saved.get("run_id") or task.id,
            "repo": task.repo,
            "delivery": saved.get("delivery", "push"),
            "local_path": local_path,
            "work_branch": task.work_branch,
        }

    def claim(self, *, worker_id: str = "") -> dict[str, Any] | None:
        if self.remote_mode:
            return self._remote_claim()
        try:
            return self.queue.claim(worker_id=worker_id or self.worker_id)
        except TypeError:
            return self.queue.claim()

    def complete(
        self,
        job_or_run_id: str | dict[str, Any],
        *,
        status: str = "completed",
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any] | None:
        job = job_or_run_id if isinstance(job_or_run_id, dict) else {}
        run_id = str(job.get("run_id") or job.get("job_id") or job_or_run_id)
        if self.remote_mode:
            return self._remote_complete(
                run_id,
                attempt_no=int(job.get("attempt_no") or 0),
                lease_token=str(job.get("lease_token") or ""),
                status=status,
                result=result,
                error=error,
            )
        try:
            return self.queue.complete(
                run_id,
                attempt_no=int(job.get("attempt_no") or 0),
                lease_token=str(job.get("lease_token") or ""),
                status=status,
                result=result,
                error=error,
            )
        except TypeError:
            self.queue.complete(run_id, status=status)
            return {"run_id": run_id, "job_id": run_id, "status": status}

    def heartbeat(self, job: dict[str, Any], *, phase: str = "") -> dict[str, Any]:
        payload = self._lease_payload(job)
        payload["phase"] = phase
        if self.remote_mode:
            url = f"{self.settings.local_worker_orchestrator_url}/v1/worker/jobs/heartbeat"
            return self._http_post(url, payload, headers=self._auth_headers())
        return self.queue.heartbeat(**payload)

    def cancel(self, run_id: str, *, reason: str = "cancelled by user") -> dict[str, Any] | None:
        """Invalidate a queued/running local job; the next heartbeat fences its process."""
        if self.remote_mode:
            url = f"{self.settings.local_worker_orchestrator_url}/v1/worker/jobs/cancel"
            return self._http_post(
                url,
                {"run_id": run_id, "job_id": run_id, "reason": reason},
                headers=self._auth_headers(),
            )
        return self.queue.cancel(run_id, reason=reason)

    def event(
        self,
        job: dict[str, Any],
        *,
        sequence: int,
        event_type: str,
        phase: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        event = {
            **self._lease_payload(job),
            "sequence": sequence,
            "event_type": event_type,
            "phase": phase,
            "payload": payload,
        }
        if self.remote_mode:
            url = f"{self.settings.local_worker_orchestrator_url}/v1/worker/jobs/events"
            return self._http_post(url, event, headers=self._auth_headers())
        return self.queue.append_event(**event)

    @staticmethod
    def _lease_payload(job: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": str(job.get("run_id") or job.get("job_id") or ""),
            "attempt_no": int(job.get("attempt_no") or 0),
            "lease_token": str(job.get("lease_token") or ""),
        }

    def _auth_headers(self) -> dict[str, str]:
        token = (self.settings.local_worker_token or "").strip()
        if not token:
            raise RuntimeError("LOCAL_WORKER_TOKEN is required for remote worker mode")
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _remote_claim(self) -> dict[str, Any] | None:
        url = f"{self.settings.local_worker_orchestrator_url}/v1/worker/jobs/claim"
        data = self._http_post(url, {"worker_id": self.worker_id}, headers=self._auth_headers())
        job = data.get("job") if isinstance(data, dict) else None
        return dict(job) if isinstance(job, dict) else None

    def _remote_complete(
        self,
        run_id: str,
        *,
        attempt_no: int,
        lease_token: str,
        status: str,
        result: dict[str, Any] | None,
        error: str,
    ) -> dict[str, Any] | None:
        if not run_id:
            return None
        url = f"{self.settings.local_worker_orchestrator_url}/v1/worker/jobs/complete"
        return self._http_post(
            url,
            {
                "run_id": run_id,
                "job_id": run_id,
                "attempt_no": attempt_no,
                "lease_token": lease_token,
                "status": status,
                "result": result or {},
                "error": error,
            },
            headers=self._auth_headers(),
        )

    @staticmethod
    def _http_post(url: str, payload: dict[str, Any], *, headers: dict[str, str]) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP POST {url} failed {exc.code}: {detail}") from exc
