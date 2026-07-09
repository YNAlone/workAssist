import json
import unittest
from pathlib import Path

from feishu_claude_automation.config import Settings
from feishu_claude_automation.models import RiskLevel, TaskRequest, TaskStatus
from feishu_claude_automation.orchestrator import Orchestrator
from feishu_claude_automation.parser import parse_command
from feishu_claude_automation.policy import Policy


def build_settings(tmp_path: Path) -> Settings:
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


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path(self._testMethodName + "_data")
        self.tmp_path.mkdir(exist_ok=True)
        self.settings = build_settings(self.tmp_path)

    def tearDown(self) -> None:
        for path in self.tmp_path.glob("*"):
            path.unlink()
        self.tmp_path.rmdir()

    def test_parse_command(self) -> None:
        request = parse_command('/ai-fix repo=acme/demo branch=main desc="Fix refund bug"')
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.repo, "acme/demo")
        self.assertEqual(request.base_branch, "main")
        self.assertEqual(request.prompt, "Fix refund bug")

    def test_policy_high_risk(self) -> None:
        policy = Policy.load(self.settings.policy_file)
        self.assertEqual(policy.classify_risk("please delete old files"), RiskLevel.HIGH)

    def test_create_low_risk_task(self) -> None:
        orchestrator = Orchestrator(self.settings)
        task = orchestrator.create_task(
            TaskRequest(repo="acme/demo", prompt="Fix refund rounding bug", requester_id="u1", chat_id="c1")
        )
        self.assertEqual(task.status, TaskStatus.DISPATCHED)
        self.assertTrue(task.work_branch.startswith("ai/feishu-"))

    def test_high_risk_requires_approval(self) -> None:
        orchestrator = Orchestrator(self.settings)
        task = orchestrator.create_task(
            TaskRequest(repo="acme/demo", prompt="delete legacy module", requester_id="u1", chat_id="c1")
        )
        self.assertEqual(task.status, TaskStatus.PENDING_APPROVAL)

    def test_runner_callback_updates_pr(self) -> None:
        orchestrator = Orchestrator(self.settings)
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
        self.assertEqual(updated.status, TaskStatus.PR_CREATED)
        self.assertTrue(updated.pr_url.endswith("/pull/1"))


if __name__ == "__main__":
    unittest.main()
