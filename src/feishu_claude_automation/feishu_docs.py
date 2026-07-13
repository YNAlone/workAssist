from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

from .feishu import FeishuClient


@dataclass(frozen=True)
class FeishuDocResult:
    token: str
    url: str


class FeishuDocService:
    """Import markdown as a Feishu docx cloud document and grant viewer access."""

    def __init__(self, client: FeishuClient, *, mount_key: str = "") -> None:
        self.client = client
        self.mount_key = mount_key

    def import_markdown(
        self,
        *,
        title: str,
        markdown: str,
        requester_open_id: str = "",
        chat_id: str = "",
    ) -> FeishuDocResult:
        if self.client.settings.dry_run:
            return FeishuDocResult(
                token="dry_run_doc",
                url="https://example.feishu.cn/docx/dry_run_doc",
            )

        content = markdown.encode("utf-8")
        file_name = f"{title}.md" if not title.endswith(".md") else title
        file_token = self._upload_markdown(file_name, content)
        ticket = self._create_import_task(file_token, title=title.replace(".md", ""))
        result = self._poll_import_task(ticket)
        self._grant_access(
            result.token,
            requester_open_id=requester_open_id,
            chat_id=chat_id,
        )
        return result

    def _auth_headers(self) -> dict[str, str]:
        token = self.client.get_tenant_access_token()
        if not token:
            raise RuntimeError("Feishu tenant access token is missing")
        return {"Authorization": f"Bearer {token}"}

    def _upload_markdown(self, file_name: str, content: bytes) -> str:
        fields = {
            "file_name": file_name,
            "parent_type": "ccm_import_open",
            "parent_node": "",
            "size": str(len(content)),
            "extra": json.dumps({"obj_type": "docx", "file_extension": "md"}, ensure_ascii=False),
        }
        result = self._post_multipart(
            "https://open.feishu.cn/open-apis/drive/v1/medias/upload_all",
            fields=fields,
            file_field="file",
            file_name=file_name,
            file_bytes=content,
        )
        file_token = result.get("data", {}).get("file_token", "")
        if not file_token:
            raise RuntimeError(f"Feishu upload failed: {result}")
        return file_token

    def _create_import_task(self, file_token: str, *, title: str) -> str:
        payload = {
            "file_extension": "md",
            "file_token": file_token,
            "type": "docx",
            "file_name": title or "分析报告",
            "point": {"mount_type": 1, "mount_key": self.mount_key},
        }
        result = self.client._request(
            "https://open.feishu.cn/open-apis/drive/v1/import_tasks",
            payload,
            headers=self._auth_headers(),
        )
        ticket = result.get("data", {}).get("ticket", "")
        if not ticket:
            raise RuntimeError(f"Feishu import task failed: {result}")
        return ticket

    def _poll_import_task(self, ticket: str, *, attempts: int = 30, interval: float = 1.0) -> FeishuDocResult:
        url = f"https://open.feishu.cn/open-apis/drive/v1/import_tasks/{urllib.parse.quote(ticket)}"
        for _ in range(attempts):
            result = self._get_json(url)
            item = result.get("data", {}).get("result", {})
            status = item.get("job_status")
            if status == 0:
                doc_token = item.get("token", "")
                doc_url = item.get("url", "")
                if not doc_url and doc_token:
                    doc_url = f"https://feishu.cn/docx/{doc_token}"
                if doc_token and doc_url:
                    return FeishuDocResult(token=doc_token, url=doc_url)
                raise RuntimeError(f"Feishu import succeeded without doc url: {result}")
            if status not in {1, 2, None}:
                raise RuntimeError(
                    f"Feishu import failed: status={status} msg={item.get('job_error_msg')}"
                )
            time.sleep(interval)
        raise RuntimeError(f"Feishu import timed out for ticket={ticket}")

    def _grant_access(
        self,
        doc_token: str,
        *,
        requester_open_id: str,
        chat_id: str,
    ) -> None:
        members: list[tuple[str, str]] = []
        if requester_open_id:
            members.append(("openid", requester_open_id))
        if chat_id:
            members.append(("openchat", chat_id))
        for member_type, member_id in members:
            try:
                self.client._request(
                    f"https://open.feishu.cn/open-apis/drive/v1/permissions/{doc_token}/members"
                    f"?type=docx",
                    {
                        "member_type": member_type,
                        "member_id": member_id,
                        "perm": "view",
                        "type": "user" if member_type == "openid" else "chat",
                    },
                    headers=self._auth_headers(),
                )
            except RuntimeError:
                # Permission grant is best-effort; doc link may still work for app owner.
                continue

    def _get_json(self, url: str) -> dict[str, Any]:
        if self.client.settings.dry_run:
            return {"dry_run": True, "url": url}
        request = urllib.request.Request(url, headers=self._auth_headers(), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu API error {exc.code}: {detail}") from exc

    def _post_multipart(
        self,
        url: str,
        *,
        fields: dict[str, str],
        file_field: str,
        file_name: str,
        file_bytes: bytes,
    ) -> dict[str, Any]:
        if self.client.settings.dry_run:
            return {"dry_run": True, "url": url, "fields": fields}

        boundary = f"----FeishuForm{uuid.uuid4().hex}"
        body = bytearray()
        for key, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'.encode()
        )
        body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        body.extend(file_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode())

        headers = self._auth_headers()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        request = urllib.request.Request(url, data=bytes(body), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Feishu API error {exc.code}: {detail}") from exc
        if result.get("code", 0) != 0:
            raise RuntimeError(f"Feishu API error: {result}")
        return result
