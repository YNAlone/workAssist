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
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["risk_level"] = self.risk_level.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
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
        )
