from __future__ import annotations

from typing import Any

from .config import Settings
from .local_worker_client import LocalWorkerClient
from .models import Task
from .policy import Policy
from .vcs import VcsDispatcher


class ExecutorDispatcher:
    """Route tasks to VCS CI pipelines or the on-machine local worker."""

    def __init__(self, settings: Settings, policy: Policy) -> None:
        self.settings = settings
        self.policy = policy
        self.vcs = VcsDispatcher(settings, policy)
        self.local_worker = LocalWorkerClient(settings, policy)

    def prepare_task(self, task: Task) -> Task:
        task.executor = self.policy.resolve_executor(repo=task.repo, executor_hint=task.executor)
        task.delivery = self.policy.resolve_delivery(repo=task.repo, delivery_hint=task.delivery)
        return task

    def dispatch(self, task: Task) -> dict[str, Any]:
        task = self.prepare_task(task)
        executor = task.executor

        if executor == "local_worker":
            local_path = self.policy.local_path_for(task.repo)
            if not local_path:
                raise RuntimeError(f"No local_path configured for repo: {task.repo}")
            return self.local_worker.enqueue(
                task,
                local_path=local_path,
                provider=self.policy.provider_for(task.repo),
            )

        if executor in {"github_actions", "gitlab_ci", "vcs"}:
            return self.vcs.dispatch(task)

        raise RuntimeError(f"Unsupported executor: {executor}")
