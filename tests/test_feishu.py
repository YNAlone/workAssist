import unittest
from unittest.mock import patch

from feishu_claude_automation.config import Settings
from feishu_claude_automation.feishu import FeishuClient, TOKEN_REFRESH_BUFFER_SECONDS


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
        orch_llm_api_key="",
        orch_llm_base_url="https://api.kimi.com/coding/",
        orch_llm_model="kimi-for-coding",
        session_store_path=__import__("pathlib").Path("/tmp/sessions.json"),
        session_ttl_minutes=120,
        local_worker_enabled=False,
        local_worker_token="",
        local_worker_queue_path=__import__("pathlib").Path("/tmp/local_worker_queue.json"),
        local_worker_poll_seconds=5,
        local_worker_orchestrator_url="",
        anthropic_api_key="",
        anthropic_base_url="https://api.kimi.com/coding/",
        anthropic_model="kimi-for-coding",
    )
    return base if not overrides else Settings(**{**base.__dict__, **overrides})


class FeishuClientTokenTest(unittest.TestCase):
    def test_reuses_token_before_expiry(self) -> None:
        client = FeishuClient(build_settings())
        with patch.object(client, "_request", return_value={
            "code": 0,
            "tenant_access_token": "token-a",
            "expire": 7200,
        }) as request_mock:
            first = client.get_tenant_access_token()
            second = client.get_tenant_access_token()
        self.assertEqual(first, "token-a")
        self.assertEqual(second, "token-a")
        request_mock.assert_called_once()

    def test_refreshes_token_after_expiry(self) -> None:
        client = FeishuClient(build_settings())
        now = 1000.0
        with patch("feishu_claude_automation.feishu.time.monotonic", side_effect=[
            now,
            now + 7200,
            now + 7200,
        ]):
            with patch.object(client, "_request", side_effect=[
                {"code": 0, "tenant_access_token": "token-a", "expire": 7200},
                {"code": 0, "tenant_access_token": "token-b", "expire": 7200},
            ]) as request_mock:
                self.assertEqual(client.get_tenant_access_token(), "token-a")
                self.assertEqual(client.get_tenant_access_token(), "token-b")
        self.assertEqual(request_mock.call_count, 2)

    def test_refreshes_token_inside_buffer_window(self) -> None:
        client = FeishuClient(build_settings())
        now = 2000.0
        elapsed = 7200 - TOKEN_REFRESH_BUFFER_SECONDS + 1
        with patch("feishu_claude_automation.feishu.time.monotonic", side_effect=[
            now,
            now + elapsed,
            now + elapsed,
        ]):
            with patch.object(client, "_request", side_effect=[
                {"code": 0, "tenant_access_token": "token-a", "expire": 7200},
                {"code": 0, "tenant_access_token": "token-b", "expire": 7200},
            ]) as request_mock:
                self.assertEqual(client.get_tenant_access_token(), "token-a")
                self.assertEqual(client.get_tenant_access_token(), "token-b")
        self.assertEqual(request_mock.call_count, 2)

    def test_send_text_retries_once_on_invalid_token(self) -> None:
        client = FeishuClient(build_settings())
        invalid = RuntimeError(
            'Feishu API error 400: {"code":99991663,"msg":"Invalid access token"}'
        )
        with patch.object(client, "_request", side_effect=[invalid, {"code": 0}]) as request_mock:
            with patch.object(client, "_fetch_tenant_access_token", side_effect=["old-token", "new-token"]):
                result = client.send_text("oc_chat", "hello")
        self.assertEqual(result, {"code": 0})
        self.assertEqual(request_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()
