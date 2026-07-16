from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

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
        self.queue = LocalWorkerQueue(settings.local_worker_queue_path)

    @property
    def remote_mode(self) -> bool:
        return bool(self.settings.local_worker_orchestrator_url)

    def enqueue(self, task: Task, *, local_path: str, provider: str) -> dict[str, Any]:
        if not self.settings.local_worker_enabled and not self.settings.dry_run:
            raise RuntimeError("LOCAL_WORKER_ENABLED is false")

        prompt = GitHubClient._wrap_prompt(task)
        job = {
            "job_id": task.id,
            "repo": task.repo,
            "prompt": prompt,
            "base_branch": task.base_branch,
            "work_branch": task.work_branch,
            "mode": task.mode.value,
            "delivery": task.delivery or "push",
            "provider": provider,
            "local_path": local_path,
            "callback_url": f"{self.settings.callback_base_url.rstrip('/')}/callbacks/runner",
        }
        if self.settings.dry_run:
            return {"dry_run": True, "executor": "local_worker", **job}
        saved = self.queue.enqueue(job)
        return {
            "executor": "local_worker",
            "job_id": task.id,
            "repo": task.repo,
            "delivery": saved.get("delivery", "push"),
            "local_path": local_path,
        }

    def claim(self) -> dict[str, Any] | None:
        if self.remote_mode:
            return self._remote_claim()
        return self.queue.claim()

    def complete(self, job_id: str, *, status: str = "completed") -> None:
        if self.remote_mode:
            self._remote_complete(job_id, status=status)
            return
        self.queue.complete(job_id, status=status)

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
        data = self._http_post(url, {}, headers=self._auth_headers())
        job = data.get("job") if isinstance(data, dict) else None
        return dict(job) if isinstance(job, dict) else None

    def _remote_complete(self, job_id: str, *, status: str) -> None:
        if not job_id:
            return
        url = f"{self.settings.local_worker_orchestrator_url}/v1/worker/jobs/complete"
        self._http_post(
            url,
            {"job_id": job_id, "status": status},
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
