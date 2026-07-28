from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_platform.db import create_db_engine, create_session_factory, init_db
from agent_platform.errors import StaleLeaseError
from agent_platform.store import PlatformStore
from agent_platform.worker_store import DurableWorkerQueue
from feishu_claude_automation.config import Settings
from feishu_claude_automation.local_worker import LocalWorkerRunner


def build_settings(tmp_path: Path, *, dry_run: bool = True) -> Settings:
    """Build an isolated worker configuration for deterministic tests."""
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "allowed_repos": ["acme/demo"],
                "repo_catalog": {
                    "acme/demo": {
                        "provider": "github",
                        "executor": "local_worker",
                        "local_path": str(tmp_path / "source"),
                        "verify_commands": ["python -m pytest -q"],
                    }
                },
                "protected_branches": ["main"],
                "allowed_requesters": [],
                "require_approval_for_risk": [],
                "high_risk_keywords": [],
                "max_concurrent_jobs": 2,
                "default_base_branch": "main",
                "work_branch_prefix": "ai/test",
                "default_executor": "local_worker",
            }
        ),
        encoding="utf-8",
    )
    return Settings(
        host="127.0.0.1",
        port=0,
        callback_base_url="http://127.0.0.1:9",
        dry_run=dry_run,
        feishu_verification_token="",
        feishu_app_id="",
        feishu_app_secret="",
        feishu_bot_webhook="",
        feishu_doc_mount_key="",
        feishu_doc_mount_folder="test",
        github_token="token",
        github_workflow_id="workflow.yml",
        github_api_base="https://api.github.com",
        github_dispatch_ref="main",
        gitlab_token="",
        gitlab_api_base="https://gitlab.example/api/v4",
        gitlab_dispatch_ref="main",
        policy_file=policy_file,
        task_store_path=tmp_path / "tasks.json",
        audit_log_path=tmp_path / "audit.log",
        orch_llm_api_key="test",
        orch_llm_base_url="https://example.invalid",
        orch_llm_model="kimi-for-coding",
        session_store_path=tmp_path / "sessions.json",
        session_ttl_minutes=120,
        local_worker_enabled=True,
        local_worker_token="worker-token",
        local_worker_queue_path=tmp_path / "queue.json",
        local_worker_poll_seconds=1,
        local_worker_orchestrator_url="",
        anthropic_api_key="test",
        anthropic_base_url="https://example.invalid",
        anthropic_model="kimi-for-coding",
        database_url="",
        local_worker_id="worker-a",
        local_worker_lease_seconds=45,
        local_worker_heartbeat_seconds=10,
        local_worker_max_recoveries=1,
        local_worker_worktree_root=tmp_path / "worktrees",
        local_worker_log_root=tmp_path / "logs",
        local_worker_log_retention_days=14,
        local_worker_max_repair_loops=2,
    )


@pytest.fixture()
def durable(tmp_path: Path) -> tuple[PlatformStore, DurableWorkerQueue]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'state.db'}")
    init_db(engine)
    factory = create_session_factory(engine)
    return PlatformStore(factory), DurableWorkerQueue(factory, lease_seconds=45, max_recoveries=1)


def test_chat_message_and_command_idempotency(
    durable: tuple[PlatformStore, DurableWorkerQueue],
) -> None:
    store, _ = durable
    task, created = store.get_or_create_chat_task(
        tenant_key="tenant-a",
        chat_id="chat-a",
        goal="first",
        requester_id="user-a",
    )
    same, created_again = store.get_or_create_chat_task(
        tenant_key="tenant-a",
        chat_id="chat-a",
        goal="duplicate",
        requester_id="user-b",
    )
    assert created is True
    assert created_again is False
    assert same.id == task.id

    inbox = store.register_inbound_message(
        tenant_key="tenant-a",
        chat_id="chat-a",
        event_id="event-1",
        message_id="message-1",
        requester_id="user-a",
        payload={"hello": "world"},
    )
    duplicate = store.register_inbound_message(
        tenant_key="tenant-a",
        chat_id="chat-a",
        event_id="event-1",
        message_id="message-1",
        requester_id="user-a",
        payload={"hello": "again"},
    )
    assert inbox["duplicate"] is False
    assert duplicate["duplicate"] is True
    assert duplicate["task_id"] == task.id

    run, run_created = store.create_run_idempotent(
        task_id=task.id,
        command_key="confirm:message-1",
        agent_id="code",
        goal="change code",
        inputs={"repo": "acme/demo"},
    )
    same_run, run_created_again = store.create_run_idempotent(
        task_id=task.id,
        command_key="confirm:message-1",
        agent_id="code",
        goal="change code twice",
        inputs={"repo": "acme/demo"},
    )
    assert run_created is True
    assert run_created_again is False
    assert same_run.id == run.id


def test_worker_lease_fencing_events_and_idempotent_complete(
    durable: tuple[PlatformStore, DurableWorkerQueue],
) -> None:
    store, queue = durable
    task, _ = store.get_or_create_chat_task(
        tenant_key="tenant-a",
        chat_id="chat-a",
        goal="work",
    )
    run, _ = store.create_run_idempotent(
        task_id=task.id,
        command_key="confirm-1",
        agent_id="code",
        goal="work",
        inputs={},
    )
    first_enqueue = queue.enqueue({"task_id": task.id, "run_id": run.id, "prompt": "work"})
    repeated_enqueue = queue.enqueue({"task_id": task.id, "run_id": run.id, "prompt": "ignored"})
    assert first_enqueue["id"] == repeated_enqueue["id"]

    lease = queue.claim(worker_id="worker-a")
    assert lease is not None
    assert lease["attempt_no"] == 1
    heartbeat = queue.heartbeat(
        run.id,
        attempt_no=lease["attempt_no"],
        lease_token=lease["lease_token"],
        phase="running_claude",
    )
    assert heartbeat["phase"] == "running_claude"
    event = queue.append_event(
        run.id,
        attempt_no=lease["attempt_no"],
        lease_token=lease["lease_token"],
        sequence=1,
        event_type="phase",
        phase="verifying",
        payload={"claude_session_id": "session-1"},
    )
    duplicate_event = queue.append_event(
        run.id,
        attempt_no=lease["attempt_no"],
        lease_token=lease["lease_token"],
        sequence=1,
        event_type="phase",
        phase="verifying",
        payload={"claude_session_id": "session-1"},
    )
    assert event["duplicate"] is False
    assert duplicate_event["duplicate"] is True

    completed = queue.complete(
        run.id,
        attempt_no=lease["attempt_no"],
        lease_token=lease["lease_token"],
        status="completed",
        result={"commit_sha": "abc", "pr_url": "https://example.test/mr/1"},
    )
    replay = queue.complete(
        run.id,
        attempt_no=lease["attempt_no"],
        lease_token=lease["lease_token"],
        status="completed",
    )
    assert completed["terminal"] is True
    assert replay["status"] == "completed"
    with pytest.raises(StaleLeaseError):
        queue.complete(
            run.id,
            attempt_no=lease["attempt_no"],
            lease_token="old-token",
            status="completed",
        )


def test_worker_safe_retry_reuses_run(
    durable: tuple[PlatformStore, DurableWorkerQueue],
) -> None:
    store, queue = durable
    task, _ = store.get_or_create_chat_task(
        tenant_key="tenant-a",
        chat_id="chat-retry",
        goal="work",
    )
    run, _ = store.create_run_idempotent(
        task_id=task.id,
        command_key="confirm-retry",
        agent_id="code",
        goal="work",
        inputs={},
    )
    queue.enqueue({"task_id": task.id, "run_id": run.id})
    first = queue.claim(worker_id="worker-a")
    assert first is not None
    queued = queue.complete(
        run.id,
        attempt_no=first["attempt_no"],
        lease_token=first["lease_token"],
        status="awaiting_retry",
        result={"claude_session_id": "session-retry"},
        error="temporary network failure",
    )
    assert queued["status"] == "queued"
    second = queue.claim(worker_id="worker-b")
    assert second is not None
    assert second["run_id"] == run.id
    assert second["attempt_no"] == 2
    assert second["recovery"] is True
    assert second["result"]["claude_session_id"] == "session-retry"


def test_cancel_invalidates_worker_lease(
    durable: tuple[PlatformStore, DurableWorkerQueue],
) -> None:
    store, queue = durable
    task, _ = store.get_or_create_chat_task(
        tenant_key="tenant-a",
        chat_id="chat-cancel",
        goal="work",
    )
    run, _ = store.create_run_idempotent(
        task_id=task.id,
        command_key="confirm-cancel",
        agent_id="code",
        goal="work",
        inputs={},
    )
    queue.enqueue({"task_id": task.id, "run_id": run.id})
    lease = queue.claim(worker_id="worker-a")
    assert lease is not None
    cancelled = queue.cancel(run.id)
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    with pytest.raises(StaleLeaseError):
        queue.heartbeat(
            run.id,
            attempt_no=lease["attempt_no"],
            lease_token=lease["lease_token"],
        )


def test_commit_recovery_detects_run_commit(tmp_path: Path) -> None:
    runner = LocalWorkerRunner(build_settings(tmp_path))
    with (
        patch.object(
            runner,
            "_run",
            side_effect=["deadbeef", "feat: run-1 automated change"],
        ),
        patch.object(runner, "_is_clean", return_value=True),
    ):
        commit_sha, no_changes = runner._commit_changes(
            tmp_path,
            run_id="run-1",
        )
    assert commit_sha == "deadbeef"
    assert no_changes is False


def test_claude_stream_json_is_logged_and_resumed(tmp_path: Path) -> None:
    settings = build_settings(tmp_path, dry_run=False)
    runner = LocalWorkerRunner(settings)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    stream = (
        '{"type":"system","session_id":"session-new"}\n'
        '{"type":"result","result":{"ok":true},"session_id":"session-new"}\n'
    )

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.StringIO(stream)
            self.returncode = 0

        def wait(self) -> int:
            return self.returncode

        def poll(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -1

    with patch("feishu_claude_automation.local_worker.subprocess.Popen", return_value=FakeProcess()) as popen:
        session_id = runner._run_claude(
            worktree,
            "fix it",
            model="kimi-for-coding",
            session_id="session-old",
            job={"task_id": "task-1", "run_id": "run-1", "attempt_no": 1},
        )
    args = popen.call_args.args[0]
    assert session_id == "session-new"
    assert "--output-format" in args
    assert "stream-json" in args
    assert "--resume" in args
    assert "--dangerously-skip-permissions" not in args
    raw_log = settings.local_worker_log_root / "task-1" / "run-1" / "attempt-1.jsonl"
    assert "session-new" in raw_log.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_persistent_worktree_is_reused_per_task(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    runner = LocalWorkerRunner(settings)
    remote = tmp_path / "remote.git"
    source = tmp_path / "source"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(source)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=source, check=True)
    (source / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=source, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=source, check=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=source, check=True, capture_output=True)

    first = runner._prepare_worktree(
        source,
        task_id="task-1",
        mode="create",
        base_branch="main",
        work_branch="ai/test-task-1",
    )
    second = runner._prepare_worktree(
        source,
        task_id="task-1",
        mode="iterate",
        base_branch="main",
        work_branch="ai/test-task-1",
    )
    assert first == second
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=first,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "ai/test-task-1"
