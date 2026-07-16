from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import Settings
from .github import GitHubClient
from .models import Task


class GitLabClient:
    """Dispatch Feishu automation jobs to GitLab CI pipelines."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.settings.dry_run:
            return {"dry_run": True, "method": method, "url": url, "payload": payload}

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "PRIVATE-TOKEN": self.settings.gitlab_token,
        }
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitLab API error {exc.code}: {detail}") from exc

    def dispatch_pipeline(self, task: Task) -> dict[str, Any]:
        if not self.settings.gitlab_token and not self.settings.dry_run:
            raise RuntimeError("GITLAB_TOKEN is required to dispatch GitLab pipelines")

        project = urllib.parse.quote(task.repo, safe="")
        callback_url = f"{self.settings.callback_base_url.rstrip('/')}/callbacks/runner"
        url = f"{self.settings.gitlab_api_base.rstrip('/')}/projects/{project}/pipeline"
        dispatch_ref = self.settings.gitlab_dispatch_ref or task.base_branch
        prompt = GitHubClient._wrap_prompt(task)
        payload = {
            "ref": dispatch_ref,
            "variables": [
                {"key": "FEISHU_JOB_ID", "value": task.id},
                {"key": "FEISHU_PROMPT", "value": prompt},
                {"key": "FEISHU_BASE_BRANCH", "value": task.base_branch},
                {"key": "FEISHU_WORK_BRANCH", "value": task.work_branch},
                {"key": "FEISHU_CALLBACK_URL", "value": callback_url},
                {"key": "FEISHU_MODE", "value": task.mode.value},
            ],
        }
        result = self._request("POST", url, payload)
        return {
            "provider": "gitlab",
            "pipeline_id": result.get("id", ""),
            "repo": task.repo,
            "job_id": task.id,
            "ref": dispatch_ref,
        }
