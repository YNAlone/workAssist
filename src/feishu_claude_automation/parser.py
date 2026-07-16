from __future__ import annotations

import json
import re

from .models import TaskRequest


FIELD_PATTERN = re.compile(r'(\w+)=(".*?"|\'.*?\'|\S+)')


def strip_feishu_mentions(text: str) -> str:
    return re.sub(r"@_user_\d+\s*", "", text).strip()


def parse_command(text: str) -> TaskRequest | None:
    # Feishu group messages often prefix mentions like "@_user_1 /ai-fix ..."
    stripped = strip_feishu_mentions(text)
    if "/ai-fix" not in stripped:
        return None
    stripped = stripped[stripped.index("/ai-fix") :]
    if not stripped.startswith("/ai-fix"):
        return None

    body = stripped[len("/ai-fix") :].strip()
    fields: dict[str, str] = {}
    for match in FIELD_PATTERN.finditer(body):
        key = match.group(1)
        value = match.group(2).strip("\"'")
        fields[key] = value

    remaining = FIELD_PATTERN.sub("", body).strip()
    prompt = fields.get("desc") or remaining
    if not fields.get("repo") or not prompt or not fields.get("branch"):
        return None

    return TaskRequest(
        repo=fields["repo"],
        prompt=prompt,
        base_branch=fields["branch"],
        requester_id=fields.get("requester", ""),
        chat_id=fields.get("chat", ""),
        issue=fields.get("issue", ""),
        work_branch=fields.get("work_branch", ""),
        executor=fields.get("executor", ""),
        delivery=fields.get("delivery", ""),
    )


def extract_message_text(payload: dict) -> str:
    event = payload.get("event", {})
    message = event.get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return content
        return parsed.get("text", content)
    return str(content)


def looks_like_confirmation(text: str) -> bool:
    cleaned = strip_feishu_mentions(text).strip().lower()
    tokens = ("确认", "执行", "开始", "可以", "没问题", "ok", "yes", "approve", "批准", "同意")
    return any(token in cleaned for token in tokens)


def looks_like_cancel(text: str) -> bool:
    cleaned = strip_feishu_mentions(text).strip().lower()
    return any(token in cleaned for token in ("取消", "算了", "停止", "cancel", "stop"))
