import unittest
from types import SimpleNamespace

from feishu_claude_automation.cards import build_confirm_plan_card, build_task_card
from feishu_claude_automation.feishu_long_connection import FeishuLongConnection
from feishu_claude_automation.models import ConversationSession, RiskLevel, Task, TaskMode, TaskStatus


class ImmediateExecutor:
    """Test executor that runs submitted work synchronously."""

    def submit(self, callback, *args):
        callback(*args)


class RecordingHandler:
    def __init__(self):
        self.calls = []

    def handle_feishu_message(self, payload, *, trusted=False):
        self.calls.append((payload, trusted))
        return {"ok": True}


class SDKEvent:
    """Small stand-in for an SDK event model exposing to_dict()."""

    def to_dict(self):
        return {
            "header": {"event_id": "evt_1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_1"}},
                "message": {
                    "message_id": "om_1",
                    "chat_id": "oc_1",
                    "message_type": "text",
                    "content": '{"text":"hello"}',
                },
            },
        }


class FeishuLongConnectionTests(unittest.TestCase):
    def test_event_is_normalized_and_dispatched_as_trusted(self):
        handler = RecordingHandler()
        listener = FeishuLongConnection(
            SimpleNamespace(),
            handler,
            executor=ImmediateExecutor(),
        )

        result = listener._on_message(SDKEvent())

        self.assertIsNone(result)
        self.assertEqual(len(handler.calls), 1)
        payload, trusted = handler.calls[0]
        self.assertTrue(trusted)
        self.assertEqual(payload["header"]["event_type"], "im.message.receive_v1")
        self.assertEqual(payload["event"]["message"]["message_id"], "om_1")

    def test_event_body_without_envelope_is_wrapped(self):
        payload = FeishuLongConnection.to_event_payload(
            {"message": {"message_id": "om_2", "message_type": "text"}}
        )

        self.assertEqual(payload["header"]["event_type"], "im.message.receive_v1")
        self.assertEqual(payload["event"]["message"]["message_id"], "om_2")

    def test_non_interactive_cards_expose_text_confirmation_only(self):
        task = Task(
            id="task_1",
            repo="acme/demo",
            prompt="delete stale code",
            base_branch="main",
            work_branch="ai/feishu-task_1",
            requester_id="ou_1",
            chat_id="oc_1",
            status=TaskStatus.PENDING_APPROVAL,
            risk_level=RiskLevel.HIGH,
            mode=TaskMode.CREATE,
        )
        task_card = build_task_card(task, interactive=False)
        session_card = build_confirm_plan_card(
            ConversationSession.create(chat_id="oc_1", requester_id="ou_1"),
            interactive=False,
        )

        self.assertFalse(any(item.get("tag") == "action" for item in task_card["elements"]))
        self.assertFalse(any(item.get("tag") == "action" for item in session_card["elements"]))
        self.assertIn("确认执行", str(task_card))
        self.assertIn("确认执行", str(session_card))


if __name__ == "__main__":
    unittest.main()
