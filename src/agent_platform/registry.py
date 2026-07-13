from __future__ import annotations

import json
from pathlib import Path

from .errors import AgentNotFoundError
from .models import AgentKind, AgentSpec


class AgentRegistry:
    def __init__(self, agents: list[AgentSpec]) -> None:
        self._agents = {agent.id: agent for agent in agents}

    @classmethod
    def from_file(cls, path: str | Path) -> AgentRegistry:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        agents: list[AgentSpec] = []
        for item in data.get("agents", []):
            agents.append(
                AgentSpec(
                    id=item["id"],
                    kind=AgentKind(item["kind"]),
                    display_name=item.get("display_name", item["id"]),
                    executor=item["executor"],
                    feishu_app_id_env=item.get("feishu_app_id_env", ""),
                    allowed_tools=list(item.get("allowed_tools") or []),
                    policy_ref=item.get("policy_ref", ""),
                )
            )
        return cls(agents)

    def get(self, agent_id: str) -> AgentSpec:
        agent = self._agents.get(agent_id)
        if not agent:
            raise AgentNotFoundError(f"agent not found: {agent_id}")
        return agent

    def list(self) -> list[AgentSpec]:
        return list(self._agents.values())

    def by_kind(self, kind: AgentKind) -> list[AgentSpec]:
        return [a for a in self._agents.values() if a.kind == kind]
