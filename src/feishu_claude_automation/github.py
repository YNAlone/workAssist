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
        # workflow YAML must exist on this ref; prefer explicit dispatch ref (e.g. default branch)
        dispatch_ref = self.settings.github_dispatch_ref or task.base_branch
        prompt = self._wrap_prompt(task)
        payload = {
            "ref": dispatch_ref,
            "inputs": {
                "job_id": task.id,
                "prompt": prompt,
                "base_branch": task.base_branch,
                "work_branch": task.work_branch,
                "callback_url": callback_url,
                "mode": task.mode.value,
            },
        }
        self._request("POST", url, payload)
        return {"workflow": self.settings.github_workflow_id, "repo": task.repo, "job_id": task.id}

    @staticmethod
    def _wrap_prompt(task: Task) -> str:
        """Ensure analysis-only work is persisted as markdown for Feishu delivery."""
        return (
            f"{task.prompt.rstrip()}\n\n"
            "---\n"
            "## 输出约束（必须遵守）\n"
            f"1. 若本任务主要是分析 / 说明 / 问答，且不需要修改业务代码，请把完整结论写入 "
            f"`docs/analysis-{task.id}.md`（Markdown 中文），不要只在对话中回复。\n"
            "2. 写入该文件后保存；后续 CI 会自动 commit / push，并由飞书机器人回传内容。\n"
            "3. 若需要修改代码，照常改代码；同时可将说明性结论写入上述 md 文件。\n"
            "4. 分支名必须与用户给定完全一致，不要臆造或拼错分支。\n"
        )
