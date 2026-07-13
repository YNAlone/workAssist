import dataclasses
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from feishu_claude_automation.config import Settings
from feishu_claude_automation.llm import IntentResult, LLMClient, extract_json_object
from feishu_claude_automation.github import GitHubClient
from feishu_claude_automation.models import (
    ConversationSession,
    RiskLevel,
    Task,
    TaskMode,
    TaskRequest,
    TaskStatus,
)
from feishu_claude_automation.orchestrator import Orchestrator
from feishu_claude_automation.parser import parse_command
from feishu_claude_automation.policy import Policy


def build_settings(tmp_path: Path) -> Settings:
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
                "default_base_branch": "",
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
        github_dispatch_ref="dev_test",
        policy_file=policy_file,
        task_store_path=tmp_path / "tasks.json",
        audit_log_path=tmp_path / "audit.log",
        orch_llm_api_key="",
        orch_llm_base_url="https://api.kimi.com/coding/",
        orch_llm_model="kimi-for-coding",
        session_store_path=tmp_path / "sessions.json",
        session_ttl_minutes=120,
    )


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmpdir.name)
        self.settings = build_settings(self.tmp_path)

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

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

    def test_resolve_repo_short_name(self) -> None:
        policy = Policy.load(self.settings.policy_file)
        self.assertEqual(policy.resolve_repo("workAssist"), "YNAlone/workAssist")
        self.assertEqual(policy.resolve_repo("demo"), "acme/demo")

    def test_create_low_risk_task(self) -> None:
        orchestrator = Orchestrator(self.settings)
        task = orchestrator.create_task(
            TaskRequest(
                repo="acme/demo",
                prompt="Fix refund rounding bug",
                base_branch="main",
                requester_id="u1",
                chat_id="c1",
            )
        )
        self.assertEqual(task.status, TaskStatus.DISPATCHED)
        self.assertTrue(task.work_branch.startswith("ai/feishu-"))
        self.assertEqual(task.mode, TaskMode.CREATE)

    def test_high_risk_requires_approval(self) -> None:
        orchestrator = Orchestrator(self.settings)
        task = orchestrator.create_task(
            TaskRequest(
                repo="acme/demo",
                prompt="delete legacy module",
                base_branch="main",
                requester_id="u1",
                chat_id="c1",
            )
        )
        self.assertEqual(task.status, TaskStatus.PENDING_APPROVAL)

    def test_runner_callback_updates_pr(self) -> None:
        orchestrator = Orchestrator(self.settings)
        task = orchestrator.create_task(
            TaskRequest(
                repo="acme/demo",
                prompt="Fix refund rounding bug",
                base_branch="main",
                requester_id="u1",
                chat_id="c1",
            )
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

    def test_extract_json_object(self) -> None:
        data = extract_json_object('```json\n{"action":"clarify","confidence":0.5}\n```')
        self.assertEqual(data["action"], "clarify")

    def test_llm_mock_confirm_plan(self) -> None:
        client = LLMClient(self.settings)
        session = ConversationSession.create(chat_id="c1", requester_id="u1")
        intent = client.interpret(
            user_text="帮我在 workAssist 项目中基于 dev_test 创建一个 devTT 分支，然后新增登录功能",
            session=session,
            allowed_repos=["YNAlone/workAssist"],
            default_base_branch="",
        )
        self.assertEqual(intent.action, "confirm_plan")
        self.assertEqual(intent.repo, "YNAlone/workAssist")
        self.assertEqual(intent.base_branch, "dev_test")
        self.assertEqual(intent.work_branch_hint, "devTT")
        self.assertTrue(intent.prompt)

    def test_llm_mock_asks_base_branch(self) -> None:
        client = LLMClient(self.settings)
        session = ConversationSession.create(chat_id="c1", requester_id="u1")
        intent = client.interpret(
            user_text="帮我在 workAssist 项目中创建一个 devTT 分支，然后新增登录功能",
            session=session,
            allowed_repos=["YNAlone/workAssist"],
            default_base_branch="",
        )
        self.assertEqual(intent.action, "clarify")
        self.assertIn("base_branch", intent.missing_fields)

    def test_natural_language_flow_and_iterate(self) -> None:
        orchestrator = Orchestrator(self.settings)

        first = orchestrator.handle_feishu_message(
            {
                "header": {"event_type": "im.message.receive_v1", "token": "token"},
                "event": {
                    "sender": {"sender_id": {"open_id": "u1"}},
                    "message": {
                        "chat_id": "c1",
                        "message_id": "m1",
                        "content": json.dumps(
                            {"text": "帮我在 workAssist 项目中基于 dev_test 创建一个 devTT 分支，然后新增登录功能"},
                            ensure_ascii=False,
                        ),
                    },
                },
            }
        )
        self.assertEqual(first.get("action"), "confirm_plan")
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
        self.assertIn("task_id", confirm)
        task = orchestrator.get_task(confirm["task_id"])
        assert task is not None
        self.assertEqual(task.work_branch, "devTT")
        self.assertEqual(task.base_branch, "dev_test")
        self.assertEqual(task.repo, "YNAlone/workAssist")
        self.assertEqual(task.status, TaskStatus.DISPATCHED)

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
        self.assertEqual(iterate.get("action"), "iterate")
        iter_task = orchestrator.get_task(iterate["task_id"])
        assert iter_task is not None
        self.assertEqual(iter_task.mode, TaskMode.ITERATE)
        self.assertEqual(iter_task.work_branch, "devTT")
        self.assertEqual(iter_task.parent_task_id, task.id)

    def test_ai_fix_command_still_works(self) -> None:
        orchestrator = Orchestrator(self.settings)
        result = orchestrator.handle_feishu_message(
            {
                "header": {"event_type": "im.message.receive_v1", "token": "token"},
                "event": {
                    "sender": {"sender_id": {"open_id": "u1"}},
                    "message": {
                        "chat_id": "c1",
                        "message_id": "m1",
                        "content": json.dumps(
                            {"text": '/ai-fix repo=acme/demo branch=main desc="Fix refund bug"'},
                            ensure_ascii=False,
                        ),
                    },
                },
            }
        )
        self.assertEqual(result.get("mode"), "command")
        task = orchestrator.get_task(result["task_id"])
        assert task is not None
        self.assertEqual(task.status, TaskStatus.DISPATCHED)


    def test_runner_callback_delivers_report_markdown_dry_run(self) -> None:
        settings = dataclasses.replace(
            self.settings,
            feishu_bot_webhook="https://example.invalid/feishu-hook",
        )
        orchestrator = Orchestrator(settings)
        task = orchestrator.create_task(
            TaskRequest(
                repo="acme/demo",
                prompt="分析模块边界",
                base_branch="main",
                requester_id="u1",
                chat_id="c_report",
                message_id="m_report",
            )
        )
        report_body = "# 分析结论\n模块划分清晰。"
        with patch.object(orchestrator, "_reply_text", wraps=orchestrator._reply_text) as reply_mock:
            updated = orchestrator.handle_runner_callback(
                {
                    "job_id": task.id,
                    "status": "completed",
                    "summary": "分析完成",
                    "report_markdown": report_body,
                    "report_path": f"docs/analysis-{task.id}.md",
                }
            )
        reply_mock.assert_called_once()
        chat_id, text, message_id = reply_mock.call_args[0]
        self.assertEqual(chat_id, "c_report")
        self.assertEqual(message_id, "m_report")
        self.assertIn("模块划分清晰", text)
        self.assertEqual(updated.status, TaskStatus.PR_CREATED)
        self.assertEqual(updated.summary, "分析完成")

    def test_github_wrap_prompt_includes_analysis_md(self) -> None:
        task = Task(
            id="job-42",
            repo="acme/demo",
            prompt="请分析架构",
            base_branch="main",
            work_branch="ai/test",
            requester_id="u1",
            chat_id="c1",
            status=TaskStatus.DISPATCHED,
            risk_level=RiskLevel.LOW,
        )
        wrapped = GitHubClient._wrap_prompt(task)
        self.assertIn("docs/analysis-job-42.md", wrapped)

    def test_intent_result_invalid_action_falls_back(self) -> None:
        intent = IntentResult.from_dict({"action": "unknown", "reply_to_user": "hi"})
        self.assertEqual(intent.action, "clarify")


if __name__ == "__main__":
    unittest.main()
