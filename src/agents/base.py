from __future__ import annotations

from typing import Protocol

from agent_platform.models import PlatformTask


class Executor(Protocol):
    def can_handle(self, task: PlatformTask) -> bool: ...

    def dispatch(self, task: PlatformTask) -> None:
        """Trigger async/sync execution; results come back via callback or inline update."""

    def on_callback(self, payload: dict) -> PlatformTask:
        """Map backend callback payload into an updated PlatformTask."""
