import json
from pathlib import Path

import pytest

from feishu_claude_automation.config import Settings
from feishu_claude_automation.models import TaskRequest, TaskStatus
from feishu_claude_automation.orchestrator import Orchestrator
from feishu_claude_automation.parser import parse_command
from feishu_claude_automation.policy import Policy


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "allowed_repos": ["acme/demo"],
                "protected_branches": ["main"],
                "allowed_requesters": [],
                "require_approval_for_risk": ["high"],
                "high_risk_keywords": ["delete"],
                "max_concurrent_jobs": 2,
                "default_base_branch": "main",
                "work_branch_prefix": "ai/feishu",
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        host="127.0.0.1",
        port=18080,
        callback_base_url="http://localhost:18080",
        dry_run=True,
        feishu_verification_token="token",
        feishu_app_id="",
        feishu_app_secret="",
        feishu_bot_webhook="",
        github_token="",
        github_workflow_id="feishu-claude.yml",
        github_api_base="https://api.github.com",
        policy_file=policy_file,
        task_store_path=tmp_path / "tasks.json",
        audit_log_path=tmp_path / "audit.log",
    )


def test_parse_command():
    request = parse_command('/ai-fix repo=acme/demo branch=main desc="Fix refund bug"')
    assert request is not None
    assert request.repo == "acme/demo"
    assert request.base_branch == "main"
    assert request.prompt == "Fix refund bug"


def test_policy_high_risk(settings: Settings):
    policy = Policy.load(settings.policy_file)
    assert policy.classify_risk("please delete old files") == RiskLevel.HIGH


def test_create_low_risk_task(settings: Settings):
    orchestrator = Orchestrator(settings)
    task = orchestrator.create_task(
        TaskRequest(repo="acme/demo", prompt="Fix refund rounding bug", requester_id="u1", chat_id="c1")
    )
    assert task.status == TaskStatus.DISPATCHED
    assert task.work_branch.startswith("ai/feishu-")


def test_high_risk_requires_approval(settings: Settings):
    orchestrator = Orchestrator(settings)
    task = orchestrator.create_task(
        TaskRequest(repo="acme/demo", prompt="delete legacy module", requester_id="u1", chat_id="c1")
    )
    assert task.status == TaskStatus.PENDING_APPROVAL


def test_runner_callback_updates_pr(settings: Settings):
    orchestrator = Orchestrator(settings)
    task = orchestrator.create_task(
        TaskRequest(repo="acme/demo", prompt="Fix refund rounding bug", requester_id="u1", chat_id="c1")
    )
    updated = orchestrator.handle_runner_callback(
        {
            "job_id": task.id,
            "status": "pr_created",
            "pr_url": "https://github.com/acme/demo/pull/1",
            "summary": "done",
        }
    )
    assert updated.status == TaskStatus.PR_CREATED
    assert updated.pr_url.endswith("/pull/1")
