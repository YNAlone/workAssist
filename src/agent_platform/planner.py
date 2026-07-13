from __future__ import annotations

import re
from typing import Any

from .models import AgentKind, PlatformTask


class Planner:
    """M1 heuristic planner: route explicit agent_id or infer code/doc from goal."""

    DOC_HINTS = ("文档", "docx", "飞书文档", "wiki", "readme.md 之外的文档", "云文档")
    CODE_HINTS = ("仓库", "分支", "pr", "pull request", "github", "代码", "repo")

    def plan_tasks(
        self,
        *,
        job_id: str,
        goal: str,
        requester_id: str = "",
        chat_id: str = "",
        preferred_agent: str = "",
        inputs: dict[str, Any] | None = None,
    ) -> list[PlatformTask]:
        inputs = dict(inputs or {})
        agent_id = preferred_agent or self._infer_agent(goal, inputs)
        task = PlatformTask.create(
            job_id=job_id,
            agent_id=agent_id,
            goal=goal,
            inputs=inputs,
            chat_id=chat_id,
            requester_id=requester_id,
        )
        return [task]

    def _infer_agent(self, goal: str, inputs: dict[str, Any]) -> str:
        if inputs.get("doc_url") or inputs.get("doc_token"):
            return AgentKind.DOC.value
        if inputs.get("repo") or inputs.get("base_branch"):
            return AgentKind.CODE.value

        lowered = goal.lower()
        doc_score = sum(1 for h in self.DOC_HINTS if h.lower() in lowered or h in goal)
        code_score = sum(1 for h in self.CODE_HINTS if h.lower() in lowered or h in goal)
        if doc_score > code_score:
            return AgentKind.DOC.value

        # Extract repo-like token owner/name if present
        if re.search(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", goal):
            return AgentKind.CODE.value
        return AgentKind.CODE.value
