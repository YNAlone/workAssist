from __future__ import annotations

from typing import Any

from .config import Settings
from .github import GitHubClient
from .gitlab import GitLabClient
from .models import Task
from .policy import Policy


class VcsDispatcher:
    """Route task dispatch to GitHub Actions or GitLab CI by repo provider."""

    def __init__(self, settings: Settings, policy: Policy) -> None:
        self.settings = settings
        self.policy = policy
        self.github = GitHubClient(settings)
        self.gitlab = GitLabClient(settings)

    def dispatch(self, task: Task) -> dict[str, Any]:
        provider = self.policy.provider_for(task.repo)
        if provider == "gitlab":
            return self.gitlab.dispatch_pipeline(task)
        return self.github.dispatch_workflow(task)
