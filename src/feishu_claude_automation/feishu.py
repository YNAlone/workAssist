from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .config import Settings
from .models import Task


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tenant_token: str = ""

    def verify_token(self, token: str) -> bool:
        expected = self.settings.feishu_verification_token
        return not expected or token == expected

    def _request(self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
        if self.settings.dry_run:
            return {"dry_run": True, "url": url, "payload": payload}

        body = json.dumps(payload).encode("utf-8")
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu API error {exc.code}: {detail}") from exc

    def get_tenant_access_token(self) -> str:
        if self._tenant_token:
            return self._tenant_token
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret:
            return ""
        result = self._request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {
                "app_id": self.settings.feishu_app_id,
                "app_secret": self.settings.feishu_app_secret,
            },
        )
        self._tenant_token = result.get("tenant_access_token", "")
        return self._tenant_token

    def send_card(self, chat_id: str, card: dict[str, Any]) -> dict[str, Any]:
        token = self.get_tenant_access_token()
        if token and chat_id:
            return self._request(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                {
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        if self.settings.feishu_bot_webhook:
            return self._request(
                self.settings.feishu_bot_webhook,
                {"msg_type": "interactive", "card": card},
            )
        return {"skipped": True, "reason": "no feishu credentials"}

    def send_text(self, chat_id: str, text: str, reply_to_message_id: str = "") -> dict[str, Any]:
        if not chat_id:
            return {"skipped": True, "reason": "missing chat_id"}
        if not text:
            return {"skipped": True, "reason": "empty text"}

        token = self.get_tenant_access_token()
        content = json.dumps({"text": text}, ensure_ascii=False)

        if token:
            if reply_to_message_id:
                return self._request(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_to_message_id}/reply",
                    {"content": content, "msg_type": "text"},
                    headers={"Authorization": f"Bearer {token}"},
                )
            return self._request(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                {
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": content,
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        if self.settings.feishu_bot_webhook:
            return self._request(
                self.settings.feishu_bot_webhook,
                {"msg_type": "text", "content": {"text": text}},
            )
        return {"skipped": True, "reason": "no feishu credentials"}

    def notify_task(self, task: Task, card: dict[str, Any]) -> dict[str, Any]:
        if not task.chat_id:
            return {"skipped": True, "reason": "missing chat_id"}
        return self.send_card(task.chat_id, card)
