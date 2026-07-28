from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agents.code_agent import CodeAgentExecutor
from agents.doc_agent import DocAgentExecutor
from agent_platform.bus import TaskBus
from agent_platform.db import create_db_engine, create_session_factory, init_db, ping_db
from agent_platform.orchestra import Orchestra
from agent_platform.registry import AgentRegistry
from agent_platform.store import PlatformStore
from feishu_claude_automation.config import Settings
from feishu_claude_automation.policy import Policy


def _default_agents_file() -> Path:
    return Path(os.getenv("AGENTS_FILE", Path(__file__).resolve().parents[2] / "config" / "agents.json"))


@dataclass
class PlatformApp:
    settings: Settings
    orchestra: Orchestra
    bus: TaskBus
    store: PlatformStore
    registry: AgentRegistry
    engine: object

    def health(self) -> dict:
        db_ok = False
        db_error = ""
        try:
            db_ok = ping_db(self.engine)
        except Exception as exc:  # noqa: BLE001
            db_error = str(exc)
        return {
            "status": "ok" if db_ok else "degraded",
            "platform": "agent_platform",
            "db_ok": db_ok,
            "db_error": db_error,
            "agents": [a.id for a in self.registry.list()],
        }


def build_platform_app(settings: Settings | None = None) -> PlatformApp:
    settings = settings or Settings.from_env()
    database_url = settings.database_url or os.getenv("DATABASE_URL")
    engine = create_db_engine(database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    store = PlatformStore(session_factory)
    registry = AgentRegistry.from_file(_default_agents_file())
    policy = Policy.load(settings.policy_file)

    code_executor = CodeAgentExecutor(settings, store, policy=policy)
    doc_executor = DocAgentExecutor()
    executors = {
        "code": code_executor,
        "github_actions": code_executor,
        "doc": doc_executor,
        "feishu_mcp": doc_executor,
    }
    bus = TaskBus(store, registry, executors)
    orchestra = Orchestra(store, bus)
    return PlatformApp(
        settings=settings,
        orchestra=orchestra,
        bus=bus,
        store=store,
        registry=registry,
        engine=engine,
    )
