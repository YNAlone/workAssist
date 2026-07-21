from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from feishu_claude_automation.config import Settings
from feishu_claude_automation.executor_dispatcher import ExecutorDispatcher
from feishu_claude_automation.local_worker_client import LocalWorkerClient
from feishu_claude_automation.local_worker_queue import LocalWorkerQueue
from feishu_claude_automation.models import Task, TaskMode, TaskRequest, TaskStatus, RiskLevel
from feishu_claude_automation.orchestrator import Orchestrator
from feishu_claude_automation.policy import Policy


def build_settings(tmp_path: Path) -> Settings:
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(
        json.dumps(
            {
                "default_executor": "vcs",
                "allowed_repos": ["acme/demo", "YNAlone/workAssist"],
                "repo_catalog": {
                    "acme/demo": {
                        "provider": "github",
                        "executor": "local_worker",
                        "local_path": str(tmp_path / "repos" / "demo"),
                        "default_delivery": "push",
                    }
                },
                "protected_branches": ["main"],
                "allowed_requesters": [],
                "require_approval_for_risk": ["high"],
                "high_risk_keywords": ["delete"],
                "max_concurrent_jobs": 2,
                "default_base_branch": "",
                "work_branch_prefix": "ai/feishu",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "repos" / "demo").mkdir(parents=True)
    return Settings(
        host="127.0.0.1",
        port=18080,
        callback_base_url="http://localhost:18080",
        dry_run=True,
        feishu_verification_token="token",
        feishu_app_id="",
        feishu_app_secret="",
        feishu_bot_webhook="",
        feishu_doc_mount_key="",
        feishu_doc_mount_folder="test",
        github_token="gh-test",
        github_workflow_id="feishu-claude.yml",
        github_api_base="https://api.github.com",
        github_dispatch_ref="dev_test",
        gitlab_token="",
        gitlab_api_base="https://gitlab.thinkingdata.cn/api/v4",
        gitlab_dispatch_ref="main",
        policy_file=policy_file,
        task_store_path=tmp_path / "tasks.json",
        audit_log_path=tmp_path / "audit.log",
        orch_llm_api_key="",
        orch_llm_base_url="https://api.kimi.com/coding/",
        orch_llm_model="kimi-for-coding",
        session_store_path=tmp_path / "sessions.json",
        session_ttl_minutes=120,
        local_worker_enabled=True,
        local_worker_token="worker-token",
        local_worker_queue_path=tmp_path / "local_worker_queue.json",
        local_worker_poll_seconds=1,
        local_worker_orchestrator_url="",
        anthropic_api_key="sk-test",
        anthropic_base_url="https://api.kimi.com/coding/",
        anthropic_model="kimi-for-coding",
    )


class LocalWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.settings = build_settings(self.tmp_path)
        self.policy = Policy.load(self.settings.policy_file)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_policy_resolve_executor_and_delivery(self) -> None:
        self.assertEqual(
            self.policy.resolve_executor(repo="acme/demo"),
            "local_worker",
        )
        self.assertEqual(
            self.policy.resolve_executor(repo="acme/demo", executor_hint="github_actions"),
            "github_actions",
        )
        self.assertEqual(self.policy.resolve_delivery(repo="acme/demo"), "push")
        self.assertEqual(
            self.policy.resolve_delivery(repo="acme/demo", delivery_hint="local_only"),
            "local_only",
        )
        self.assertEqual(self.policy.local_path_for("acme/demo"), str(self.tmp_path / "repos" / "demo"))

    def test_queue_enqueue_and_claim(self) -> None:
        queue = LocalWorkerQueue(self.settings.local_worker_queue_path)
        queue.enqueue({"job_id": "job1", "repo": "acme/demo"})
        queue.enqueue({"job_id": "job2", "repo": "acme/demo"})
        first = queue.claim()
        second = queue.claim()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None
        assert second is not None
        self.assertEqual(first["job_id"], "job1")
        self.assertEqual(second["job_id"], "job2")
        self.assertIsNone(queue.claim())

    def test_executor_dispatcher_routes_local_worker(self) -> None:
        dispatcher = ExecutorDispatcher(self.settings, self.policy)
        task = Task(
            id="abc123",
            repo="acme/demo",
            prompt="Fix bug",
            base_branch="main",
            work_branch="ai/feishu-abc123",
            requester_id="u1",
            chat_id="c1",
            status=TaskStatus.QUEUED,
            risk_level=RiskLevel.LOW,
        )
        result = dispatcher.dispatch(task)
        self.assertEqual(result["executor"], "local_worker")
        self.assertEqual(task.executor, "local_worker")
        self.assertEqual(task.delivery, "push")

    def test_executor_dispatcher_respects_delivery_hint(self) -> None:
        settings = Settings(**{**self.settings.__dict__, "dry_run": False})
        dispatcher = ExecutorDispatcher(settings, self.policy)
        task = Task(
            id="abc124",
            repo="acme/demo",
            prompt="Analyze only",
            base_branch="main",
            work_branch="ai/feishu-abc124",
            requester_id="u1",
            chat_id="c1",
            status=TaskStatus.QUEUED,
            risk_level=RiskLevel.LOW,
            delivery="local_only",
        )
        dispatcher.dispatch(task)
        client = LocalWorkerClient(settings, self.policy)
        job = client.claim()
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job["delivery"], "local_only")

    def test_orchestrator_dispatches_local_worker_task(self) -> None:
        orchestrator = Orchestrator(self.settings)
        task = orchestrator.create_task(
            TaskRequest(
                repo="acme/demo",
                prompt="Fix refund rounding bug",
                base_branch="main",
                requester_id="u1",
                chat_id="c1",
                executor="local_worker",
            )
        )
        self.assertEqual(task.status, TaskStatus.DISPATCHED)
        self.assertEqual(task.executor, "local_worker")

    def test_runner_callback_local_only_summary(self) -> None:
        orchestrator = Orchestrator(self.settings)
        task = orchestrator.create_task(
            TaskRequest(
                repo="acme/demo",
                prompt="Analyze module",
                base_branch="main",
                requester_id="u1",
                chat_id="c1",
                executor="local_worker",
                delivery="local_only",
            )
        )
        updated = orchestrator.handle_runner_callback(
            {
                "job_id": task.id,
                "status": "succeeded",
                "delivery": "local_only",
                "worktree_path": "/data/repos/demo",
                "diff_stat": " src/app.py | 2 ++",
                "summary": "本机工作区已更新（未推远程）",
            }
        )
        self.assertEqual(updated.status, TaskStatus.PR_CREATED)
        self.assertIn("本机工作区", updated.summary)
        self.assertIn("变更摘要", updated.summary)

    def test_remote_claim_and_complete(self) -> None:
        settings = Settings(
            **{
                **self.settings.__dict__,
                "dry_run": False,
                "local_worker_orchestrator_url": "http://orchestrator.example:8080",
            }
        )
        client = LocalWorkerClient(settings, self.policy)

        with patch.object(
            client,
            "_http_post",
            side_effect=[
                {"job": {"job_id": "remote-1", "repo": "acme/demo"}},
                {"job_id": "remote-1", "status": "completed"},
            ],
        ) as mock_post:
            job = client.claim()
            self.assertIsNotNone(job)
            assert job is not None
            self.assertEqual(job["job_id"], "remote-1")
            client.complete("remote-1", status="completed")
            self.assertEqual(mock_post.call_count, 2)
            claim_url = mock_post.call_args_list[0].args[0]
            complete_url = mock_post.call_args_list[1].args[0]
            self.assertTrue(claim_url.endswith("/v1/worker/jobs/claim"))
            self.assertTrue(complete_url.endswith("/v1/worker/jobs/complete"))

    @patch("feishu_claude_automation.local_worker.subprocess.run")
    def test_local_worker_runner_local_only(self, mock_run) -> None:
        from feishu_claude_automation.local_worker import LocalWorkerRunner

        class RunResult:
            def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(args, **kwargs):
            if args[:2] == ["git", "status"]:
                return RunResult(stdout=" M src/app.py\n")
            if args[:2] == ["git", "diff"]:
                return RunResult(stdout=" src/app.py | 2 ++\n")
            return RunResult(stdout="ok")

        mock_run.side_effect = fake_run

        client = LocalWorkerClient(self.settings, self.policy)
        job = {
            "job_id": "job-local",
            "repo": "acme/demo",
            "prompt": "analyze",
            "base_branch": "main",
            "work_branch": "ai/feishu-job-local",
            "mode": TaskMode.CREATE.value,
            "delivery": "local_only",
            "provider": "github",
            "local_path": str(self.tmp_path / "repos" / "demo"),
            "callback_url": "http://localhost:18080/callbacks/runner",
        }
        client.queue.enqueue(job)

        runner = LocalWorkerRunner(self.settings)
        with patch.object(runner, "_run_claude"), patch.object(runner, "_callback") as callback:
            runner.execute_job(job)
            callback.assert_called()
            payload = callback.call_args[0][1]
            self.assertEqual(payload["status"], "succeeded")
            self.assertEqual(payload["delivery"], "local_only")
            self.assertIn("diff_stat", payload)


if __name__ == "__main__":
    unittest.main()
