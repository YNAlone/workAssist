from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_platform.app import build_platform_app
from agent_platform.db import create_db_engine, create_session_factory, init_db
from agent_platform.models import PlatformTaskStatus
from agent_platform.orchestra import Orchestra
from agent_platform.planner import Planner
from agent_platform.registry import AgentRegistry
from agent_platform.store import PlatformStore
from feishu_claude_automation.config import Settings


@pytest.fixture()
def pg_url() -> str:
    return "postgresql+psycopg://agent:agent@127.0.0.1:5432/agent_platform"


@pytest.fixture()
def store(pg_url: str) -> PlatformStore:
    engine = create_db_engine(pg_url)
    init_db(engine)
    return PlatformStore(create_session_factory(engine))


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "allowed_repos": ["YNAlone/workAssist", "acme/demo"],
                "protected_branches": ["main", "master"],
                "allowed_requesters": [],
                "require_approval_for_risk": ["high"],
                "high_risk_keywords": ["delete"],
                "max_concurrent_jobs": 5,
                "default_base_branch": "",
                "work_branch_prefix": "ai/dev",
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        host="127.0.0.1",
        port=0,
        callback_base_url="http://127.0.0.1:9",
        dry_run=True,
        feishu_verification_token="",
        feishu_app_id="",
        feishu_app_secret="",
        feishu_bot_webhook="",
        feishu_doc_mount_key="",
        feishu_doc_mount_folder="test",
        github_token="",
        github_workflow_id="feishu-claude.yml",
        github_api_base="https://api.github.com",
        github_dispatch_ref="dev_test",
        policy_file=policy_file,
        task_store_path=tmp_path / "tasks.json",
        audit_log_path=tmp_path / "audit.log",
        session_store_path=tmp_path / "sessions.json",
        session_ttl_minutes=120,
        orch_llm_api_key="",
        orch_llm_base_url="https://api.kimi.com/coding/",
        orch_llm_model="kimi-for-coding",
    )


def test_registry_loads_agents():
    registry = AgentRegistry.from_file(Path("config/agents.json"))
    assert {a.id for a in registry.list()} == {"code", "doc"}
    assert registry.get("code").executor == "github_actions"


def test_planner_prefers_code_for_repo_goal():
    planner = Planner()
    tasks = planner.plan_tasks(job_id="j1", goal="在 YNAlone/workAssist 基于 dev_test 改 README")
    assert len(tasks) == 1
    assert tasks[0].agent_id == "code"


def test_planner_prefers_doc_when_doc_url():
    planner = Planner()
    tasks = planner.plan_tasks(
        job_id="j1",
        goal="更新需求说明",
        inputs={"doc_url": "https://feishu.cn/docx/abc"},
    )
    assert tasks[0].agent_id == "doc"


def test_orchestra_dispatches_code_job_dry_run(settings: Settings, monkeypatch, pg_url: str):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("AGENTS_FILE", str(Path("config/agents.json").resolve()))
    app = build_platform_app(settings)
    result = app.orchestra.create_and_dispatch(
        goal="修复 refund 舍入问题",
        agent_id="code",
        inputs={"repo": "acme/demo", "base_branch": "main", "prompt": "Fix refund rounding"},
        requester_id="u1",
        chat_id="c1",
    )
    job = result["job"]
    tasks = result["tasks"]
    assert job["id"]
    assert len(tasks) == 1
    assert tasks[0]["agent_id"] == "code"
    assert tasks[0]["status"] in {
        PlatformTaskStatus.RUNNING.value,
        PlatformTaskStatus.DISPATCHED.value,
        PlatformTaskStatus.FAILED.value,
    }

    bundle = app.orchestra.get_job_bundle(job["id"])
    assert bundle["job"]["id"] == job["id"]
    assert len(bundle["tasks"]) == 1


def test_callback_updates_platform_task(settings: Settings, monkeypatch, pg_url: str):
    monkeypatch.setenv("DATABASE_URL", pg_url)
    monkeypatch.setenv("AGENTS_FILE", str(Path("config/agents.json").resolve()))
    app = build_platform_app(settings)
    created = app.orchestra.create_and_dispatch(
        goal="fix readme",
        agent_id="code",
        inputs={"repo": "acme/demo", "base_branch": "main", "prompt": "update readme"},
    )
    task_id = created["tasks"][0]["id"]
    updated = app.bus.apply_callback(
        {
            "job_id": task_id,
            "status": "pr_created",
            "pr_url": "https://github.com/acme/demo/pull/1",
            "summary": "ok",
        }
    )
    assert updated.status == PlatformTaskStatus.SUCCEEDED
    assert updated.result.get("pr_url")
    job = app.store.require_job(created["job"]["id"])
    assert job.status.value in {"done", "running", "partial"}
