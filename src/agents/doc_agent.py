from __future__ import annotations

from agent_platform.errors import DispatchError
from agent_platform.models import PlatformTask


class DocAgentExecutor:
    """Stub for M2: Feishu remote MCP + UAT."""

    agent_id = "doc"

    def can_handle(self, task: PlatformTask) -> bool:
        return task.agent_id in {"doc", "feishu_mcp"}

    def dispatch(self, task: PlatformTask) -> None:
        raise DispatchError(
            "DocAgent is not implemented yet (M2). "
            "Configure UAT OAuth and MCP client before dispatching doc tasks."
        )

    def on_callback(self, payload: dict) -> PlatformTask:
        raise DispatchError("DocAgent callback is not implemented yet (M2)")
