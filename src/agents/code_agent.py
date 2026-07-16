from __future__ import annotations

from typing import Any

from feishu_claude_automation.config import Settings
from feishu_claude_automation.models import Task, TaskMode, TaskRequest, TaskStatus, RiskLevel
from feishu_claude_automation.policy import Policy
from feishu_claude_automation.vcs import VcsDispatcher
from agent_platform.models import PlatformTask, PlatformTaskStatus, utc_now
from agent_platform.store import PlatformStore


class CodeAgentExecutor:
    """Wraps GitHub Actions / GitLab CI dispatch as a platform executor."""

    agent_id = "code"

    def __init__(self, settings: Settings, store: PlatformStore, policy: Policy | None = None) -> None:
        self.settings = settings
        self.store = store
        self.policy = policy or Policy.load(settings.policy_file)
        self.vcs = VcsDispatcher(settings, self.policy)

    def can_handle(self, task: PlatformTask) -> bool:
        return task.agent_id in {"code", "github_actions", "gitlab_ci"} or task.inputs.get("repo")

    def _to_legacy_task(self, task: PlatformTask) -> Task:
        inputs = task.inputs
        repo = str(inputs.get("repo") or "").strip()
        if repo:
            repo = self.policy.resolve_repo(repo) or repo
        prompt = str(inputs.get("prompt") or task.goal)
        base_branch = str(inputs.get("base_branch") or "").strip()
        work_branch = str(inputs.get("work_branch") or f"ai/dev-{task.id}")
        mode_raw = str(inputs.get("mode") or TaskMode.CREATE.value)
        mode = TaskMode(mode_raw) if mode_raw in {m.value for m in TaskMode} else TaskMode.CREATE

        request = TaskRequest(
            repo=repo,
            prompt=prompt,
            base_branch=base_branch,
            work_branch=work_branch,
            requester_id=task.requester_id,
            chat_id=task.chat_id,
            session_id=task.job_id,
            mode=mode,
        )
        if not request.repo:
            raise ValueError("code task requires inputs.repo")
        if not request.base_branch:
            raise ValueError("code task requires inputs.base_branch")
        self.policy.validate_request(request)

        legacy = Task.from_request(request, work_branch=work_branch, risk_level=RiskLevel.LOW)
        # Keep platform task id as job_id for Actions callback correlation
        legacy.id = task.id
        legacy.status = TaskStatus.DISPATCHED
        return legacy

    def dispatch(self, task: PlatformTask) -> None:
        legacy = self._to_legacy_task(task)
        result = self.vcs.dispatch(legacy)
        task.status = PlatformTaskStatus.RUNNING
        task.result = {"dispatch": result, "work_branch": legacy.work_branch, "repo": legacy.repo}
        task.updated_at = utc_now()
        self.store.save_task(task)

    def on_callback(self, payload: dict[str, Any]) -> PlatformTask:
        task_id = str(payload.get("job_id") or payload.get("task_id") or "")
        task = self.store.require_task(task_id)
        status = str(payload.get("status") or "").lower()

        if status in {"running", "dispatched"}:
            task.status = PlatformTaskStatus.RUNNING
        elif status in {"pr_created", "succeeded", "success", "completed"}:
            task.status = PlatformTaskStatus.SUCCEEDED
            task.result = {
                **task.result,
                "pr_url": payload.get("pr_url", ""),
                "summary": payload.get("summary", ""),
                "commit_sha": payload.get("commit_sha", ""),
            }
            task.error = ""
        elif status in {"failed", "error", "cancelled"}:
            task.status = PlatformTaskStatus.FAILED
            task.error = str(payload.get("error") or "workflow failed")
        else:
            task.status = PlatformTaskStatus.RUNNING
            task.result = {**task.result, "raw_status": status}

        task.updated_at = utc_now()
        return task
