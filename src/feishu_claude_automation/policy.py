from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .models import RiskLevel, TaskRequest


class PolicyError(Exception):
    pass


@dataclass
class RepoInfo:
    name: str
    provider: str = "github"
    url: str = ""
    executor: str = ""
    local_path: str = ""
    default_delivery: str = "push"


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
    default_executor: str
    repo_catalog: dict[str, RepoInfo] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Policy:
        data = json.loads(path.read_text(encoding="utf-8"))
        catalog: dict[str, RepoInfo] = {}
        for name, meta in (data.get("repo_catalog") or {}).items():
            if isinstance(meta, str):
                catalog[name] = RepoInfo(name=name, provider=meta)
                continue
            catalog[name] = RepoInfo(
                name=name,
                provider=str((meta or {}).get("provider") or "github").lower(),
                url=str((meta or {}).get("url") or ""),
                executor=str((meta or {}).get("executor") or ""),
                local_path=str((meta or {}).get("local_path") or ""),
                default_delivery=str((meta or {}).get("default_delivery") or "push"),
            )
        for name in data.get("allowed_repos", []):
            catalog.setdefault(name, RepoInfo(name=name, provider="github"))
        return cls(
            allowed_repos=data.get("allowed_repos", []),
            protected_branches=data.get("protected_branches", ["main", "master"]),
            allowed_requesters=data.get("allowed_requesters", []),
            require_approval_for_risk=data.get("require_approval_for_risk", ["high"]),
            high_risk_keywords=data.get("high_risk_keywords", []),
            max_concurrent_jobs=int(data.get("max_concurrent_jobs", 3)),
            default_base_branch=data.get("default_base_branch", "main"),
            work_branch_prefix=data.get("work_branch_prefix", "ai/feishu"),
            default_executor=str(data.get("default_executor") or "vcs"),
            repo_catalog=catalog,
        )

    def provider_for(self, repo: str) -> str:
        info = self.repo_catalog.get(repo)
        if info:
            return info.provider or "github"
        return "github"

    def repo_url(self, repo: str) -> str:
        info = self.repo_catalog.get(repo)
        return info.url if info else ""

    def local_path_for(self, repo: str) -> str:
        info = self.repo_catalog.get(repo)
        return info.local_path if info else ""

    def resolve_executor(self, *, repo: str, executor_hint: str = "") -> str:
        hint = (executor_hint or "").strip().lower()
        if hint:
            return hint
        info = self.repo_catalog.get(repo)
        if info and info.executor:
            return info.executor.lower()
        default = (self.default_executor or "vcs").strip().lower()
        if default and default != "vcs":
            return default
        provider = self.provider_for(repo)
        if provider == "gitlab":
            return "gitlab_ci"
        return "github_actions"

    def resolve_delivery(self, *, repo: str, delivery_hint: str = "") -> str:
        hint = (delivery_hint or "").strip().lower()
        if hint in {"push", "local_only"}:
            return hint
        info = self.repo_catalog.get(repo)
        if info and info.default_delivery:
            return info.default_delivery.lower()
        return "push"

    def validate_request(self, request: TaskRequest) -> None:
        if self.allowed_repos and request.repo not in self.allowed_repos:
            raise PolicyError(f"Repository not allowed: {request.repo}")
        if self.allowed_requesters and request.requester_id not in self.allowed_requesters:
            raise PolicyError(f"Requester not allowed: {request.requester_id}")
        if not (request.base_branch or "").strip():
            raise PolicyError("Base branch is required")

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

    @staticmethod
    def _normalize_repo_hint(repo_hint: str) -> str:
        hint = (repo_hint or "").strip()
        if not hint:
            return ""
        if "://" in hint:
            path = urlparse(hint).path.strip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return path
        return hint.removesuffix(".git")

    def resolve_repo(self, repo_hint: str) -> str:
        hint = self._normalize_repo_hint(repo_hint)
        if not hint:
            return ""
        if not self.allowed_repos:
            return hint
        if hint in self.allowed_repos:
            return hint

        lowered = hint.lower()
        for allowed in self.allowed_repos:
            if allowed.lower() == lowered:
                return allowed
            short = allowed.split("/")[-1]
            if short.lower() == lowered:
                return allowed
            if short.lower() in lowered or lowered in short.lower():
                return allowed
            info = self.repo_catalog.get(allowed)
            if info and info.url:
                catalog_path = self._normalize_repo_hint(info.url).lower()
                if catalog_path == lowered or catalog_path.endswith("/" + lowered):
                    return allowed
        return hint

    def normalize_work_branch(self, hint: str, task_id: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9._/-]+", "-", (hint or "").strip()).strip("-/")
        if cleaned:
            self.ensure_work_branch_allowed(cleaned)
            return cleaned
        return self.build_work_branch(task_id)
