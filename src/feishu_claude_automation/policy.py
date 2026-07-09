from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .models import RiskLevel, TaskRequest


class PolicyError(Exception):
    pass


@dataclass
class Policy:
    allowed_repos: list[str]
    protected_branches: list[str]
    allowed_requesters: list[str]
    require_approval_for_risk: list[str]
    high_risk_keywords: list[str]
    max_concurrent_jobs: int
    default_base_branch: str
    work_branch_prefix: str

    @classmethod
    def load(cls, path: Path) -> Policy:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            allowed_repos=data.get("allowed_repos", []),
            protected_branches=data.get("protected_branches", ["main", "master"]),
            allowed_requesters=data.get("allowed_requesters", []),
            require_approval_for_risk=data.get("require_approval_for_risk", ["high"]),
            high_risk_keywords=data.get("high_risk_keywords", []),
            max_concurrent_jobs=int(data.get("max_concurrent_jobs", 3)),
            default_base_branch=data.get("default_base_branch", "main"),
            work_branch_prefix=data.get("work_branch_prefix", "ai/feishu"),
        )

    def validate_request(self, request: TaskRequest) -> None:
        if self.allowed_repos and request.repo not in self.allowed_repos:
            raise PolicyError(f"Repository not allowed: {request.repo}")
        if self.allowed_requesters and request.requester_id not in self.allowed_requesters:
            raise PolicyError(f"Requester not allowed: {request.requester_id}")
        if request.base_branch in self.protected_branches and request.base_branch != self.default_base_branch:
            raise PolicyError(f"Base branch not permitted: {request.base_branch}")

    def classify_risk(self, prompt: str) -> RiskLevel:
        lowered = prompt.lower()
        for keyword in self.high_risk_keywords:
            if keyword.lower() in lowered:
                return RiskLevel.HIGH
        if any(word in lowered for word in ("refactor", "rename", "migrate")):
            return RiskLevel.MEDIUM
        return RiskLevel.LOW

    def requires_approval(self, risk_level: RiskLevel) -> bool:
        return risk_level.value in self.require_approval_for_risk

    def build_work_branch(self, task_id: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9-]+", "-", task_id).strip("-").lower()
        return f"{self.work_branch_prefix}-{slug}"

    def ensure_work_branch_allowed(self, work_branch: str) -> None:
        for protected in self.protected_branches:
            if work_branch == protected:
                raise PolicyError(f"Work branch cannot equal protected branch: {protected}")
