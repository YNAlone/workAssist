from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse


DOC_URL_RE = re.compile(
    r"https?://[^\s<>\"']+(?:feishu\.cn|larksuite\.com)/(?:docx|wiki|docs|sheets|base)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)
GENERIC_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


@dataclass
class MessagePart:
    kind: str  # text | link | image | file | media | mention | doc | unknown
    text: str = ""
    url: str = ""
    key: str = ""
    name: str = ""
    mime: str = ""
    data_base64: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedFeishuMessage:
    message_type: str = "text"
    message_id: str = ""
    chat_id: str = ""
    parts: list[MessagePart] = field(default_factory=list)
    mentions: list[dict[str, Any]] = field(default_factory=list)
    raw_content: dict[str, Any] | str | None = None

    def plain_text(self) -> str:
        chunks: list[str] = []
        for part in self.parts:
            if part.kind in {"text", "mention"} and part.text:
                chunks.append(part.text)
            elif part.kind in {"link", "doc"}:
                # Prefer visible label; avoid duplicating URLs already present in text parts.
                label = part.text or part.url
                joined = " ".join(chunks)
                if part.url and part.url in joined:
                    continue
                if label and label not in joined:
                    chunks.append(label)
        return " ".join(chunks).strip()

    def is_empty(self) -> bool:
        if self.plain_text():
            return False
        return not any(part.kind in {"image", "file", "media", "doc", "unknown"} for part in self.parts)

    def to_llm_text(self) -> str:
        """Serialize all parts into a structured text envelope for the LLM."""
        lines = [
            f"[feishu_message type={self.message_type or 'unknown'} id={self.message_id or '-'}]",
        ]
        if self.mentions:
            names = []
            for item in self.mentions:
                name = str(item.get("name") or "")
                key = str(item.get("key") or "")
                names.append(f"{key}={name}" if name else key)
            lines.append(f"[mentions] {', '.join(names)}")

        for idx, part in enumerate(self.parts, start=1):
            prefix = f"[part {idx} kind={part.kind}]"
            if part.kind == "text":
                lines.append(f"{prefix}\n{part.text}")
            elif part.kind in {"link", "doc"}:
                detail = part.url or part.text
                if part.text and part.url and part.text != part.url:
                    detail = f"{part.text} -> {part.url}"
                if part.meta.get("body"):
                    detail = f"{detail}\n--- document body ---\n{part.meta['body']}"
                elif part.meta.get("fetch_error"):
                    detail = f"{detail}\n(document fetch failed: {part.meta['fetch_error']})"
                lines.append(f"{prefix}\n{detail}")
            elif part.kind == "image":
                extra = []
                if part.key:
                    extra.append(f"image_key={part.key}")
                if part.mime:
                    extra.append(f"mime={part.mime}")
                if part.data_base64:
                    extra.append("binary=attached_as_image_url")
                else:
                    extra.append("binary=unavailable")
                lines.append(f"{prefix} {' '.join(extra)}")
            elif part.kind in {"file", "media"}:
                extra = []
                if part.name:
                    extra.append(f"name={part.name}")
                if part.key:
                    extra.append(f"file_key={part.key}")
                if part.url:
                    extra.append(f"url={part.url}")
                lines.append(f"{prefix} {' '.join(extra) or '(no metadata)'}")
            elif part.kind == "mention":
                lines.append(f"{prefix} {part.text or part.name}")
            else:
                payload = part.meta.get("raw")
                lines.append(f"{prefix}\n{json.dumps(payload, ensure_ascii=False) if payload is not None else part.text}")

        return "\n\n".join(lines).strip()

    def to_llm_content(self) -> str | list[dict[str, Any]]:
        """OpenAI-compatible multimodal content when images are available; else structured text."""
        text = self.to_llm_text()
        image_parts = [p for p in self.parts if p.kind == "image" and p.data_base64]
        if not image_parts:
            return text
        blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for part in image_parts:
            mime = part.mime or "image/png"
            blocks.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{part.data_base64}"},
                }
            )
        return blocks


def _parse_content_field(content: Any) -> dict[str, Any] | str:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content
        return parsed if isinstance(parsed, (dict, list)) else content
    return str(content)


def _append_text(parts: list[MessagePart], text: str) -> None:
    cleaned = (text or "").strip()
    if cleaned:
        parts.append(MessagePart(kind="text", text=cleaned))


def _append_url(parts: list[MessagePart], *, url: str, text: str = "") -> None:
    url = (url or "").strip()
    if not url:
        return
    kind = "doc" if DOC_URL_RE.search(url) else "link"
    parts.append(MessagePart(kind=kind, url=url, text=text or url))


def _walk_post_blocks(blocks: Any, parts: list[MessagePart]) -> None:
    if not isinstance(blocks, list):
        return
    for row in blocks:
        if not isinstance(row, list):
            if isinstance(row, dict):
                _walk_post_element(row, parts)
            continue
        for el in row:
            if isinstance(el, dict):
                _walk_post_element(el, parts)


def _walk_post_element(el: dict[str, Any], parts: list[MessagePart]) -> None:
    tag = str(el.get("tag") or "")
    if tag in {"text", "md"}:
        _append_text(parts, str(el.get("text") or ""))
    elif tag == "a":
        _append_url(parts, url=str(el.get("href") or ""), text=str(el.get("text") or ""))
    elif tag == "at":
        parts.append(
            MessagePart(
                kind="mention",
                text=str(el.get("user_name") or el.get("text") or el.get("user_id") or "@user"),
                meta={"user_id": el.get("user_id")},
            )
        )
    elif tag == "img":
        parts.append(MessagePart(kind="image", key=str(el.get("image_key") or "")))
    elif tag == "media":
        parts.append(
            MessagePart(
                kind="media",
                key=str(el.get("file_key") or ""),
                meta={"image_key": el.get("image_key")},
            )
        )
    elif tag == "emotion":
        _append_text(parts, f"[emotion:{el.get('emoji_type') or ''}]")
    elif tag == "code_block":
        lang = str(el.get("language") or "")
        code = str(el.get("text") or "")
        _append_text(parts, f"```{lang}\n{code}\n```".rstrip())
    elif tag == "hr":
        _append_text(parts, "---")
    else:
        parts.append(MessagePart(kind="unknown", meta={"raw": el}))


def _extract_urls_from_text(text: str, parts: list[MessagePart]) -> None:
    for match in GENERIC_URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(")。.,，]")
        # Avoid duplicating if already captured as dedicated link/doc parts.
        if any(p.url == url for p in parts):
            continue
        _append_url(parts, url=url)


def extract_doc_token(url: str) -> tuple[str, str]:
    """Return (doc_type, token) from a Feishu doc/wiki URL."""
    path = unquote(urlparse(url).path.strip("/"))
    segments = path.split("/")
    if len(segments) >= 2 and segments[0] in {"docx", "wiki", "docs", "sheets", "base"}:
        return segments[0], segments[1]
    return "", ""


def parse_feishu_event_message(payload: dict[str, Any]) -> ParsedFeishuMessage:
    event = payload.get("event") or {}
    message = event.get("message") or {}
    message_type = str(message.get("message_type") or message.get("msg_type") or "text")
    parsed = ParsedFeishuMessage(
        message_type=message_type,
        message_id=str(message.get("message_id") or ""),
        chat_id=str(message.get("chat_id") or ""),
        mentions=list(message.get("mentions") or []),
    )
    content = _parse_content_field(message.get("content", ""))
    parsed.raw_content = content if isinstance(content, (dict, str)) else str(content)
    parts = parsed.parts

    if isinstance(content, str):
        _append_text(parts, content)
    elif message_type == "text" and isinstance(content, dict):
        _append_text(parts, str(content.get("text") or ""))
    elif message_type == "image" and isinstance(content, dict):
        parts.append(MessagePart(kind="image", key=str(content.get("image_key") or "")))
    elif message_type in {"file", "audio", "media"} and isinstance(content, dict):
        parts.append(
            MessagePart(
                kind="file" if message_type == "file" else "media",
                key=str(content.get("file_key") or ""),
                name=str(content.get("file_name") or content.get("file_name") or ""),
                meta=dict(content),
            )
        )
    elif message_type == "sticker" and isinstance(content, dict):
        parts.append(MessagePart(kind="image", key=str(content.get("file_key") or ""), meta={"sticker": True}))
    elif message_type == "post" and isinstance(content, dict):
        # Prefer content_v2 markdown when present.
        content_v2 = content.get("content_v2")
        if isinstance(content_v2, str) and content_v2.strip():
            _append_text(parts, content_v2)
        else:
            locale = content.get("zh_cn") or content.get("en_us") or content
            if isinstance(locale, dict):
                title = str(locale.get("title") or "").strip()
                if title:
                    _append_text(parts, title)
                _walk_post_blocks(locale.get("content"), parts)
            else:
                parts.append(MessagePart(kind="unknown", meta={"raw": content}))
    elif message_type in {"share_chat", "share_user"} and isinstance(content, dict):
        parts.append(MessagePart(kind="unknown", text=f"shared:{message_type}", meta={"raw": content}))
    elif isinstance(content, dict):
        # Fallback: try common fields then keep raw.
        if content.get("text"):
            _append_text(parts, str(content.get("text")))
        if content.get("image_key"):
            parts.append(MessagePart(kind="image", key=str(content.get("image_key"))))
        if content.get("file_key"):
            parts.append(
                MessagePart(
                    kind="file",
                    key=str(content.get("file_key")),
                    name=str(content.get("file_name") or ""),
                )
            )
        if not parts:
            parts.append(MessagePart(kind="unknown", meta={"raw": content}))
    else:
        parts.append(MessagePart(kind="unknown", text=str(content)))

    # Extract URLs from free text as link/doc parts for completeness.
    for part in list(parts):
        if part.kind == "text" and part.text:
            _extract_urls_from_text(part.text, parts)

    return parsed


def extract_message_text(payload: dict) -> str:
    """Backward-compatible plain text extractor used by command parsing."""
    return parse_feishu_event_message(payload).plain_text()


def enrich_message_for_llm(parsed: ParsedFeishuMessage, feishu_client: Any) -> ParsedFeishuMessage:
    """Best-effort: download images and fetch Feishu doc bodies before sending to LLM."""
    if feishu_client is None or getattr(getattr(feishu_client, "settings", None), "dry_run", False):
        return parsed

    for part in parsed.parts:
        if part.kind == "image" and part.key and not part.data_base64 and parsed.message_id:
            try:
                raw, content_type = feishu_client.download_message_resource(
                    parsed.message_id,
                    part.key,
                    resource_type="image",
                )
                if raw:
                    part.data_base64 = base64.b64encode(raw).decode("ascii")
                    part.mime = (content_type or "image/png").split(";")[0].strip()
            except Exception as exc:  # noqa: BLE001
                part.meta["fetch_error"] = str(exc)

        if part.kind == "doc" and part.url and not part.meta.get("body"):
            doc_type, token = extract_doc_token(part.url)
            if doc_type == "docx" and token:
                try:
                    body = feishu_client.fetch_docx_raw_content(token)
                    if body:
                        part.meta["body"] = body
                    else:
                        part.meta["fetch_error"] = "empty document body or missing permission"
                except Exception as exc:  # noqa: BLE001
                    part.meta["fetch_error"] = str(exc)
    return parsed
