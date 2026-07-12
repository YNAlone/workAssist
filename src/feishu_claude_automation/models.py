from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class TaskStatus(str, Enum):
    RECEIVED = "received"
    PENDING_APPROVAL = "pending_approval"
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    PR_CREATED = "pr_created"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskMode(str, Enum):
    CREATE = "create"
    ITERATE = "iterate"


class SessionStatus(str, Enum):
    IDLE = "idle"
    CLARIFYING = "clarifying"
    AWAITING_CONFIRM = "awaiting_confirm"
    AWAITING_APPROVAL = "awaiting_approval"
    RUNNING = "running"
    AWAITING_FEEDBACK = "awaiting_feedback"
    CLOSED = "closed"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskRequest:
    repo: str
    prompt: str
    base_branch: str = "main"
    requester_id: str = ""
    chat_id: str = ""
    message_id: str = ""
    issue: str = ""
    work_branch: str = ""
    session_id: str = ""
    mode: TaskMode = TaskMode.CREATE
    parent_task_id: str = ""
    iteration: int = 0


@dataclass
class Task:
    id: str
    repo: str
    prompt: str
    base_branch: str
    work_branch: str
    requester_id: str
    chat_id: str
    status: TaskStatus
    risk_level: RiskLevel
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    message_id: str = ""
    issue: str = ""
    approved_by: str = ""
    dispatch_id: str = ""
    pr_url: str = ""
    commit_sha: str = ""
    summary: str = ""
    error: str = ""
    audit: list[dict[str, Any]] = field(default_factory=list)
    session_id: str = ""
    parent_task_id: str = ""
    iteration: int = 0
    mode: TaskMode = TaskMode.CREATE

    @classmethod
    def from_request(cls, request: TaskRequest, work_branch: str, risk_level: RiskLevel) -> Task:
        return cls(
            id=uuid4().hex[:12],
            repo=request.repo,
            prompt=request.prompt,
            base_branch=request.base_branch,
            work_branch=work_branch,
            requester_id=request.requester_id,
            chat_id=request.chat_id,
            message_id=request.message_id,
            issue=request.issue,
            status=TaskStatus.RECEIVED,
            risk_level=risk_level,
            session_id=request.session_id,
            parent_task_id=request.parent_task_id,
            iteration=request.iteration,
            mode=request.mode,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["risk_level"] = self.risk_level.value
        data["mode"] = self.mode.value if isinstance(self.mode, TaskMode) else self.mode
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        mode_raw = data.get("mode", TaskMode.CREATE.value)
        return cls(
            id=data["id"],
            repo=data["repo"],
            prompt=data["prompt"],
            base_branch=data["base_branch"],
            work_branch=data["work_branch"],
            requester_id=data.get("requester_id", ""),
            chat_id=data.get("chat_id", ""),
            status=TaskStatus(data["status"]),
            risk_level=RiskLevel(data["risk_level"]),
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
            message_id=data.get("message_id", ""),
            issue=data.get("issue", ""),
            approved_by=data.get("approved_by", ""),
            dispatch_id=data.get("dispatch_id", ""),
            pr_url=data.get("pr_url", ""),
            commit_sha=data.get("commit_sha", ""),
            summary=data.get("summary", ""),
            error=data.get("error", ""),
            audit=data.get("audit", []),
            session_id=data.get("session_id", ""),
            parent_task_id=data.get("parent_task_id", ""),
            iteration=int(data.get("iteration", 0)),
            mode=TaskMode(mode_raw) if not isinstance(mode_raw, TaskMode) else mode_raw,
        )


@dataclass
class SessionMessage:
    role: str
    content: str
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMessage:
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            timestamp=data.get("timestamp", utc_now()),
        )


@dataclass
class ConversationSession:
    id: str
    chat_id: str
    requester_id: str
    status: SessionStatus = SessionStatus.IDLE
    repo: str = ""
    base_branch: str = "main"
    work_branch: str = ""
    prompt: str = ""
    current_task_id: str = ""
    pr_url: str = ""
    messages: list[SessionMessage] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def append_message(self, role: str, content: str) -> None:
        self.messages.append(SessionMessage(role=role, content=content))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "requester_id": self.requester_id,
            "status": self.status.value,
            "repo": self.repo,
            "base_branch": self.base_branch,
            "work_branch": self.work_branch,
            "prompt": self.prompt,
            "current_task_id": self.current_task_id,
            "pr_url": self.pr_url,
            "messages": [msg.to_dict() for msg in self.messages],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConversationSession:
        return cls(
            id=data["id"],
            chat_id=data.get("chat_id", ""),
            requester_id=data.get("requester_id", ""),
            status=SessionStatus(data.get("status", SessionStatus.IDLE.value)),
            repo=data.get("repo", ""),
            base_branch=data.get("base_branch", "main"),
            work_branch=data.get("work_branch", ""),
            prompt=data.get("prompt", ""),
            current_task_id=data.get("current_task_id", ""),
            pr_url=data.get("pr_url", ""),
            messages=[SessionMessage.from_dict(item) for item in data.get("messages", [])],
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
        )

    @classmethod
    def create(cls, chat_id: str, requester_id: str) -> ConversationSession:
        return cls(id=uuid4().hex[:12], chat_id=chat_id, requester_id=requester_id)
