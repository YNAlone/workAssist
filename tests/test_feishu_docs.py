import unittest
from unittest.mock import MagicMock, patch

from feishu_claude_automation.config import Settings
from feishu_claude_automation.feishu import FeishuClient
from feishu_claude_automation.feishu_docs import FeishuDocService


def build_settings(**overrides) -> Settings:
    base = Settings(
        host="127.0.0.1",
        port=8080,
        callback_base_url="http://localhost:8080",
        dry_run=False,
        feishu_verification_token="token",
        feishu_app_id="app",
        feishu_app_secret="secret",
        feishu_bot_webhook="",
        feishu_doc_mount_key="",
        github_token="",
        github_workflow_id="feishu-claude.yml",
        github_api_base="https://api.github.com",
        github_dispatch_ref="dev_test",
        policy_file=__import__("pathlib").Path("/tmp/policy.json"),
        task_store_path=__import__("pathlib").Path("/tmp/tasks.json"),
        audit_log_path=__import__("pathlib").Path("/tmp/audit.log"),
        orch_llm_api_key="",
        orch_llm_base_url="https://api.kimi.com/coding/",
        orch_llm_model="kimi-for-coding",
        session_store_path=__import__("pathlib").Path("/tmp/sessions.json"),
        session_ttl_minutes=120,
    )
    return base if not overrides else Settings(**{**base.__dict__, **overrides})


class FeishuDocServiceTest(unittest.TestCase):
    def test_import_markdown_dry_run(self) -> None:
        client = FeishuClient(build_settings(dry_run=True))
        service = FeishuDocService(client)
        result = service.import_markdown(title="demo", markdown="# hello", requester_open_id="ou_x")
        self.assertIn("feishu.cn/docx/", result.url)

    @patch.object(FeishuDocService, "_poll_import_task")
    @patch.object(FeishuDocService, "_create_import_task", return_value="ticket-1")
    @patch.object(FeishuDocService, "_upload_markdown", return_value="file-token")
    @patch.object(FeishuDocService, "_grant_access")
    def test_import_markdown_happy_path(
        self,
        grant_mock: MagicMock,
        upload_mock: MagicMock,
        create_mock: MagicMock,
        poll_mock: MagicMock,
    ) -> None:
        from feishu_claude_automation.feishu_docs import FeishuDocResult

        poll_mock.return_value = FeishuDocResult(
            token="docx_token",
            url="https://example.feishu.cn/docx/docx_token",
        )
        client = FeishuClient(build_settings(dry_run=False))
        with patch.object(client, "get_tenant_access_token", return_value="t-token"):
            service = FeishuDocService(client)
            result = service.import_markdown(
                title="分析报告",
                markdown="# 结论\n内容",
                requester_open_id="ou_user",
                chat_id="oc_chat",
            )
        self.assertEqual(result.url, "https://example.feishu.cn/docx/docx_token")
        upload_mock.assert_called_once()
        create_mock.assert_called_once_with("file-token", title="分析报告")
        grant_mock.assert_called_once()
