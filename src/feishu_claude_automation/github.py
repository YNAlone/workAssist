from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Settings
from .models import Task


class GitHubClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.settings.dry_run:
            return {"dry_run": True, "method": method, "url": url, "payload": payload}

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.settings.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API error {exc.code}: {detail}") from exc

    def dispatch_workflow(self, task: Task) -> dict[str, Any]:
        owner, repo = task.repo.split("/", 1)
        callback_url = f"{self.settings.callback_base_url.rstrip('/')}/callbacks/runner"
        url = (
            f"{self.settings.github_api_base.rstrip('/')}/repos/"
            f"{owner}/{repo}/actions/workflows/{self.settings.github_workflow_id}/dispatches"
        )
        payload = {
            "ref": task.base_branch,
            "inputs": {
                "job_id": task.id,
                "prompt": task.prompt,
                "base_branch": task.base_branch,
                "work_branch": task.work_branch,
                "callback_url": callback_url,
                "mode": task.mode.value,
            },
        }
        self._request("POST", url, payload)
        return {"workflow": self.settings.github_workflow_id, "repo": task.repo, "job_id": task.id}
