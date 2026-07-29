from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import Settings
from .models import Task

# Refresh before Feishu expiry (default 7200s) to avoid mid-request failures.
TOKEN_REFRESH_BUFFER_SECONDS = 300


class FeishuClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._tenant_token: str = ""
        self._tenant_token_expires_at: float = 0.0

    def verify_token(self, token: str) -> bool:
        expected = self.settings.feishu_verification_token
        return not expected or token == expected

    @staticmethod
    def _is_invalid_token_error(exc: BaseException) -> bool:
        message = str(exc)
        return "99991663" in message or "Invalid access token" in message

    def _invalidate_tenant_token(self) -> None:
        self._tenant_token = ""
        self._tenant_token_expires_at = 0.0

    def _tenant_token_is_valid(self) -> bool:
        if not self._tenant_token:
            return False
        return time.monotonic() < self._tenant_token_expires_at - TOKEN_REFRESH_BUFFER_SECONDS

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

    def _request_get_bytes(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> tuple[bytes, str]:
        if self.settings.dry_run:
            return b"", "application/octet-stream"
        request_headers = dict(headers or {})
        request = urllib.request.Request(url, headers=request_headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                return response.read(), content_type
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu GET error {exc.code}: {detail}") from exc

    def _request_get_json(self, url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
        if self.settings.dry_run:
            return {"dry_run": True, "url": url}
        raw, _ = self._request_get_bytes(url, headers=headers)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _fetch_tenant_access_token(self) -> str:
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret:
            return ""

        result = self._request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {
                "app_id": self.settings.feishu_app_id,
                "app_secret": self.settings.feishu_app_secret,
            },
        )
        if result.get("code", 0) != 0:
            raise RuntimeError(f"Feishu tenant token failed: {result}")

        token = result.get("tenant_access_token", "")
        if not token:
            raise RuntimeError(f"Feishu tenant token missing in response: {result}")

        expire = float(result.get("expire", 7200))
        self._tenant_token = token
        self._tenant_token_expires_at = time.monotonic() + expire
        return token

    def get_tenant_access_token(self, *, force_refresh: bool = False) -> str:
        if not force_refresh and self._tenant_token_is_valid():
            return self._tenant_token
        self._invalidate_tenant_token()
        return self._fetch_tenant_access_token()

    def _auth_headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        token = self.get_tenant_access_token(force_refresh=force_refresh)
        if not token:
            return {}
        return {"Authorization": f"Bearer {token}"}

    def _request_with_bearer(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        allow_retry: bool = True,
    ) -> dict[str, Any]:
        for attempt in range(2 if allow_retry else 1):
            token = self.get_tenant_access_token(force_refresh=attempt > 0)
            if not token:
                break
            try:
                return self._request(
                    url,
                    payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except RuntimeError as exc:
                if allow_retry and attempt == 0 and self._is_invalid_token_error(exc):
                    self._invalidate_tenant_token()
                    continue
                raise
        return {}

    def download_message_resource(
        self,
        message_id: str,
        file_key: str,
        *,
        resource_type: str = "image",
    ) -> tuple[bytes, str]:
        """Download image/file binary attached to a message."""
        if not message_id or not file_key:
            raise RuntimeError("message_id and file_key are required")
        query = urllib.parse.urlencode({"type": resource_type})
        url = (
            f"https://open.feishu.cn/open-apis/im/v1/messages/"
            f"{urllib.parse.quote(message_id)}/resources/{urllib.parse.quote(file_key)}?{query}"
        )
        for attempt in range(2):
            headers = self._auth_headers(force_refresh=attempt > 0)
            if not headers:
                return b"", "application/octet-stream"
            try:
                return self._request_get_bytes(url, headers=headers)
            except RuntimeError as exc:
                if attempt == 0 and self._is_invalid_token_error(exc):
                    self._invalidate_tenant_token()
                    continue
                raise
        return b"", "application/octet-stream"

    def fetch_docx_raw_content(self, document_id: str, *, max_chars: int = 20000) -> str:
        """Fetch plain text content of a Feishu docx document."""
        if not document_id:
            return ""
        url = (
            "https://open.feishu.cn/open-apis/docx/v1/documents/"
            f"{urllib.parse.quote(document_id)}/raw_content"
        )
        for attempt in range(2):
            headers = self._auth_headers(force_refresh=attempt > 0)
            if not headers:
                return ""
            try:
                result = self._request_get_json(url, headers=headers)
            except RuntimeError as exc:
                if attempt == 0 and self._is_invalid_token_error(exc):
                    self._invalidate_tenant_token()
                    continue
                raise
            if result.get("code", 0) != 0:
                raise RuntimeError(f"Feishu docx raw_content failed: {result}")
            content = str((result.get("data") or {}).get("content") or "")
            if max_chars > 0 and len(content) > max_chars:
                return content[:max_chars] + "\n…(truncated)"
            return content
        return ""

    def send_card(self, chat_id: str, card: dict[str, Any]) -> dict[str, Any]:
        if chat_id:
            result = self._request_with_bearer(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                {
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
            )
            if result:
                return result

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

        content = json.dumps({"text": text}, ensure_ascii=False)
        if reply_to_message_id:
            result = self._request_with_bearer(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_to_message_id}/reply",
                {"content": content, "msg_type": "text"},
            )
        else:
            result = self._request_with_bearer(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                {
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": content,
                },
            )
        if result:
            return result

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
