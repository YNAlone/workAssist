import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from feishu_claude_automation.config import Settings
from feishu_claude_automation.feishu_event_dedup import FeishuEventDedup
from feishu_claude_automation.orchestrator import Orchestrator


class FeishuEventDedupTests(unittest.TestCase):
    def test_try_claim_drops_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dedup = FeishuEventDedup(Path(tmp) / "dedup.json")
            self.assertTrue(dedup.try_claim("event:abc"))
            self.assertFalse(dedup.try_claim("event:abc"))
            self.assertTrue(dedup.try_claim("event:xyz"))


class FeishuDuplicateWebhookTests(unittest.TestCase):
    def test_same_message_id_only_replies_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            policy_file = tmp_path / "policy.json"
            policy_file.write_text(
                Path(__file__).resolve().parents[1].joinpath("config/policy.example.json").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )
            settings = Settings(
                host="127.0.0.1",
                port=8080,
                callback_base_url="http://localhost:8080",
                dry_run=True,
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
                policy_file=policy_file,
                task_store_path=tmp_path / "tasks.json",
                audit_log_path=tmp_path / "audit.log",
                orch_llm_api_key="",
                orch_llm_base_url="https://api.kimi.com/coding/",
                orch_llm_model="kimi-for-coding",
                session_store_path=tmp_path / "sessions.json",
                session_ttl_minutes=120,
                local_worker_enabled=False,
                local_worker_token="",
                local_worker_queue_path=tmp_path / "queue.json",
                local_worker_poll_seconds=5,
                local_worker_orchestrator_url="",
                anthropic_api_key="",
                anthropic_base_url="https://api.kimi.com/coding/",
                anthropic_model="kimi-for-coding",
            )
            orchestrator = Orchestrator(settings)
            payload = {
                "header": {"event_type": "im.message.receive_v1", "token": "token", "event_id": "evt-dup-1"},
                "event": {
                    "sender": {"sender_id": {"open_id": "u1"}},
                    "message": {
                        "chat_id": "c1",
                        "message_id": "m-dup-1",
                        "content": json.dumps({"text": "你现在有哪些项目知识"}, ensure_ascii=False),
                    },
                },
            }
            with patch.object(orchestrator, "_reply_text") as reply_mock:
                first = orchestrator.handle_feishu_message(payload)
                second = orchestrator.handle_feishu_message(payload)
            self.assertNotIn("duplicate", first)
            self.assertEqual(second.get("reason"), "duplicate_event")
            self.assertEqual(reply_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
