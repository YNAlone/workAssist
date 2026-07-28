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
    AWAITING_RETRY = "awaiting_retry"
    NEEDS_ATTENTION = "needs_attention"
    CANCELLED = "cancelled"


class PlatformTaskStatus(str, Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_ATTENTION = "needs_attention"
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
    command_key: str = ""
    iteration: int = 1
    phase: str = "queued"
    attempt_no: int = 0
    verification: dict[str, Any] = field(default_factory=dict)
    commit_sha: str = ""
    remote_sha: str = ""
    mr_url: str = ""
    ci_status: str = ""
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
            command_key=data.get("command_key", ""),
            iteration=int(data.get("iteration", 1)),
            phase=data.get("phase", "queued"),
            attempt_no=int(data.get("attempt_no", 0)),
            verification=data.get("verification") or {},
            commit_sha=data.get("commit_sha", ""),
            remote_sha=data.get("remote_sha", ""),
            mr_url=data.get("mr_url", ""),
            ci_status=data.get("ci_status", ""),
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
    tenant_key: str = "default"
    repo: str = ""
    base_branch: str = ""
    work_branch: str = ""
    worktree_path: str = ""
    current_run_id: str = ""
    claude_session_id: str = ""
    version: int = 1
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        goal: str,
        requester_id: str = "",
        chat_id: str = "",
        tenant_key: str = "default",
    ) -> Job:
        return cls(
            id=new_id(),
            requester_id=requester_id,
            chat_id=chat_id,
            tenant_key=tenant_key,
            goal=goal,
        )

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
            tenant_key=data.get("tenant_key", "default"),
            repo=data.get("repo", ""),
            base_branch=data.get("base_branch", ""),
            work_branch=data.get("work_branch", ""),
            worktree_path=data.get("worktree_path", ""),
            current_run_id=data.get("current_run_id", ""),
            claude_session_id=data.get("claude_session_id", ""),
            version=int(data.get("version", 1)),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )
