import unittest
from unittest.mock import patch

from feishu_claude_automation.config import Settings
from feishu_claude_automation.llm import (
    IntentResult,
    LLMClient,
    extract_json_object,
    looks_like_doc_writing,
)
from feishu_claude_automation.models import ConversationSession


def _settings() -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8080,
        callback_base_url="http://localhost:8080",
        dry_run=False,
        feishu_verification_token="token",
        feishu_app_id="app",
        feishu_app_secret="secret",
        feishu_bot_webhook="",
        feishu_doc_mount_key="",
        feishu_doc_mount_folder="test",
        github_token="",
        github_workflow_id="feishu-claude.yml",
        github_api_base="https://api.github.com",
        github_dispatch_ref="dev_test",
        gitlab_token="",
        gitlab_api_base="https://gitlab.thinkingdata.cn/api/v4",
        gitlab_dispatch_ref="main",
        policy_file=__import__("pathlib").Path("/tmp/policy.json"),
        task_store_path=__import__("pathlib").Path("/tmp/tasks.json"),
        audit_log_path=__import__("pathlib").Path("/tmp/audit.log"),
        orch_llm_api_key="sk-test",
        orch_llm_base_url="https://api.kimi.com/coding/",
        orch_llm_model="kimi-for-coding",
        session_store_path=__import__("pathlib").Path("/tmp/sessions.json"),
        session_ttl_minutes=120,
        local_worker_enabled=False,
        local_worker_token="",
        local_worker_queue_path=__import__("pathlib").Path("/tmp/queue.json"),
        local_worker_poll_seconds=5,
        local_worker_orchestrator_url="",
        anthropic_api_key="",
        anthropic_base_url="https://api.kimi.com/coding/",
        anthropic_model="kimi-for-coding",
    )


class DocWritingIntentTests(unittest.TestCase):
    def test_looks_like_doc_writing(self) -> None:
        self.assertTrue(looks_like_doc_writing("给我一份完整的开发方案"))
        self.assertTrue(looks_like_doc_writing("对 frontend 做 AB Test 方案"))
        self.assertFalse(looks_like_doc_writing("修一下登录按钮样式"))

    def test_extract_json_embedded_in_prose(self) -> None:
        text = '说明如下：\n```json\n{"action":"clarify","repo":"x/y","confidence":0.5}\n```\n完'
        data = extract_json_object(text)
        self.assertEqual(data["action"], "clarify")

    def test_recover_prose_plan_to_clarify_missing_branch(self) -> None:
        client = LLMClient(_settings())
        user_text = (
            "现在想基于 CJ报名落地页ABtest方案 这个文档对 thinkingdata/official-web-frontend "
            "进行AB Test，给我一份完整的开发方案"
        )
        prose = "# 一、实验目标\n\n对比转化率……\n\n## 二、实验意义\n\n……"
        with patch.object(
            client,
            "_chat_completions",
            return_value={"choices": [{"message": {"content": prose}}]},
        ):
            intent = client.interpret(
                user_text=user_text,
                session=ConversationSession.create(chat_id="c1", requester_id="u1"),
                allowed_repos=[
                    "YNAlone/workAssist",
                    "thinkingdata/official-web-frontend",
                    "thinkingdata/official-web-server",
                ],
                default_base_branch="",
            )
        self.assertEqual(intent.action, "clarify")
        self.assertEqual(intent.repo, "thinkingdata/official-web-frontend")
        self.assertIn("base_branch", intent.missing_fields)
        self.assertIn("docs/analysis", intent.prompt)
        self.assertIn("文档/方案", intent.reply_to_user)

    def test_recover_prose_plan_to_confirm_when_complete(self) -> None:
        client = LLMClient(_settings())
        session = ConversationSession.create(chat_id="c1", requester_id="u1")
        session.repo = "thinkingdata/official-web-frontend"
        session.base_branch = "feature_6.3"
        user_text = "基于当前分支给我一份完整的 AB Test 开发方案"
        prose = "# 方案\n\n1. 实验组\n2. 对照组\n" + ("细节\n" * 40)
        with patch.object(
            client,
            "_chat_completions",
            return_value={"choices": [{"message": {"content": prose}}]},
        ):
            intent = client.interpret(
                user_text=user_text,
                session=session,
                allowed_repos=["thinkingdata/official-web-frontend"],
                default_base_branch="",
            )
        self.assertEqual(intent.action, "confirm_plan")
        self.assertIn("docs/analysis", intent.prompt)
        self.assertIn("文档/方案", intent.reply_to_user)

    def test_normalize_chitchat_json_to_doc_plan(self) -> None:
        client = LLMClient(_settings())
        session = ConversationSession.create(chat_id="c1", requester_id="u1")
        session.repo = "thinkingdata/official-web-frontend"
        session.base_branch = "feature_6.3"
        fake_json = IntentResult(
            action="chitchat",
            reply_to_user="这是一份很长的方案……",
            confidence=0.9,
        )
        content = (
            '{"action":"chitchat","reply_to_user":"这是一份很长的方案……","confidence":0.9}'
        )
        with patch.object(
            client,
            "_chat_completions",
            return_value={"choices": [{"message": {"content": content}}]},
        ):
            intent = client.interpret(
                user_text="给我一份完整的开发方案，针对 official-web-frontend",
                session=session,
                allowed_repos=["thinkingdata/official-web-frontend"],
                default_base_branch="",
            )
        self.assertEqual(intent.action, "confirm_plan")
        self.assertEqual(fake_json.action, "chitchat")  # original object untouched
        self.assertIn("docs/analysis", intent.prompt)

    def test_mock_doc_task(self) -> None:
        settings = _settings()
        settings = Settings(**{**settings.__dict__, "dry_run": True, "orch_llm_api_key": ""})
        client = LLMClient(settings)
        intent = client.interpret(
            user_text="帮我在 official-web-frontend 基于 feature_6.3 写一份完整开发方案",
            session=ConversationSession.create(chat_id="c1", requester_id="u1"),
            allowed_repos=["thinkingdata/official-web-frontend"],
            default_base_branch="",
        )
        self.assertEqual(intent.action, "confirm_plan")
        self.assertEqual(intent.repo, "thinkingdata/official-web-frontend")
        self.assertIn("docs/analysis", intent.prompt)


if __name__ == "__main__":
    unittest.main()
