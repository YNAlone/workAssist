from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid4().hex[:12]


class AgentKind(str, Enum):
    CODE = "code"
    DOC = "doc"
    ORCHESTRA = "orchestra"


class JobStatus(str, Enum):
    RECEIVED = "received"
    CLARIFYING = "clarifying"
    PLANNED = "planned"
    RUNNING = "running"
    PARTIAL = "partial"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlatformTaskStatus(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AgentSpec:
    id: str
    kind: AgentKind
    display_name: str
    executor: str
    feishu_app_id_env: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    policy_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["kind"] = self.kind.value
        return data


@dataclass
class PlatformTask:
    id: str
    job_id: str
    agent_id: str
    goal: str
    inputs: dict[str, Any] = field(default_factory=dict)
    status: PlatformTaskStatus = PlatformTaskStatus.QUEUED
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    chat_id: str = ""
    requester_id: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        job_id: str,
        agent_id: str,
        goal: str,
        inputs: dict[str, Any] | None = None,
        chat_id: str = "",
        requester_id: str = "",
    ) -> PlatformTask:
        return cls(
            id=new_id(),
            job_id=job_id,
            agent_id=agent_id,
            goal=goal,
            inputs=inputs or {},
            chat_id=chat_id,
            requester_id=requester_id,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlatformTask:
        status = data.get("status", PlatformTaskStatus.QUEUED.value)
        return cls(
            id=data["id"],
            job_id=data["job_id"],
            agent_id=data["agent_id"],
            goal=data.get("goal", ""),
            inputs=data.get("inputs") or {},
            status=PlatformTaskStatus(status) if not isinstance(status, PlatformTaskStatus) else status,
            result=data.get("result") or {},
            error=data.get("error", ""),
            chat_id=data.get("chat_id", ""),
            requester_id=data.get("requester_id", ""),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )


@dataclass
class Job:
    id: str
    requester_id: str
    chat_id: str
    goal: str
    status: JobStatus = JobStatus.RECEIVED
    task_ids: list[str] = field(default_factory=list)
    plan: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(cls, *, goal: str, requester_id: str = "", chat_id: str = "") -> Job:
        return cls(id=new_id(), requester_id=requester_id, chat_id=chat_id, goal=goal)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Job:
        status = data.get("status", JobStatus.RECEIVED.value)
        return cls(
            id=data["id"],
            requester_id=data.get("requester_id", ""),
            chat_id=data.get("chat_id", ""),
            goal=data.get("goal", ""),
            status=JobStatus(status) if not isinstance(status, JobStatus) else status,
            task_ids=list(data.get("task_ids") or []),
            plan=data.get("plan") or {},
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )
