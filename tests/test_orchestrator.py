import json
from pathlib import Path

import pytest

from feishu_claude_automation.config import Settings
from feishu_claude_automation.models import RiskLevel, TaskMode, TaskRequest, TaskStatus
from feishu_claude_automation.orchestrator import Orchestrator
from feishu_claude_automation.parser import parse_command
from feishu_claude_automation.policy import Policy


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "allowed_repos": ["acme/demo", "YNAlone/workAssist"],
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
        orch_llm_api_key="",
        orch_llm_base_url="https://api.kimi.com/coding/",
        orch_llm_model="kimi-for-coding",
        session_store_path=tmp_path / "sessions.json",
        session_ttl_minutes=120,
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


def test_resolve_repo_short_name(settings: Settings):
    policy = Policy.load(settings.policy_file)
    assert policy.resolve_repo("workAssist") == "YNAlone/workAssist"


def test_create_low_risk_task(settings: Settings):
    orchestrator = Orchestrator(settings)
    task = orchestrator.create_task(
        TaskRequest(repo="acme/demo", prompt="Fix refund rounding bug", requester_id="u1", chat_id="c1")
    )
    assert task.status == TaskStatus.DISPATCHED
    assert task.work_branch.startswith("ai/feishu-")
    assert task.mode == TaskMode.CREATE


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


def test_natural_language_confirm_and_iterate(settings: Settings):
    orchestrator = Orchestrator(settings)
    first = orchestrator.handle_feishu_message(
        {
            "header": {"event_type": "im.message.receive_v1", "token": "token"},
            "event": {
                "sender": {"sender_id": {"open_id": "u1"}},
                "message": {
                    "chat_id": "c1",
                    "message_id": "m1",
                    "content": json.dumps(
                        {"text": "帮我在 workAssist 项目中创建一个 devTT 分支，然后新增登录功能"},
                        ensure_ascii=False,
                    ),
                },
            },
        }
    )
    assert first["action"] == "confirm_plan"
    session_id = first["session_id"]

    confirm = orchestrator.handle_card_action(
        {
            "token": "token",
            "action": {
                "operator": {"open_id": "u1"},
                "value": {"action": "confirm_execute", "session_id": session_id},
            },
        }
    )
    task = orchestrator.get_task(confirm["task_id"])
    assert task is not None
    assert task.work_branch == "devTT"

    orchestrator.handle_runner_callback(
        {
            "job_id": task.id,
            "status": "pr_created",
            "pr_url": "https://github.com/YNAlone/workAssist/pull/9",
            "summary": "created",
        }
    )
    iterate = orchestrator.handle_feishu_message(
        {
            "header": {"event_type": "im.message.receive_v1", "token": "token"},
            "event": {
                "sender": {"sender_id": {"open_id": "u1"}},
                "message": {
                    "chat_id": "c1",
                    "message_id": "m2",
                    "content": json.dumps({"text": "再补一组单元测试"}, ensure_ascii=False),
                },
            },
        }
    )
    iter_task = orchestrator.get_task(iterate["task_id"])
    assert iter_task is not None
    assert iter_task.mode == TaskMode.ITERATE
    assert iter_task.work_branch == "devTT"
