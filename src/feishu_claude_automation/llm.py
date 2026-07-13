from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .models import ConversationSession


VALID_ACTIONS = {
    "clarify",
    "confirm_plan",
    "execute",
    "iterate",
    "cancel",
    "chitchat",
}


@dataclass
class IntentResult:
    action: str = "clarify"
    repo: str = ""
    base_branch: str = ""
    work_branch_hint: str = ""
    prompt: str = ""
    reply_to_user: str = ""
    missing_fields: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IntentResult:
        action = str(data.get("action", "clarify")).strip().lower()
        if action not in VALID_ACTIONS:
            action = "clarify"
        missing = data.get("missing_fields") or []
        if not isinstance(missing, list):
            missing = [str(missing)]
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return cls(
            action=action,
            repo=str(data.get("repo", "") or ""),
            base_branch=str(data.get("base_branch", "") or ""),
            work_branch_hint=str(data.get("work_branch_hint", "") or ""),
            prompt=str(data.get("prompt", "") or ""),
            reply_to_user=str(data.get("reply_to_user", "") or ""),
            missing_fields=[str(item) for item in missing],
            confidence=confidence,
        )


SYSTEM_PROMPT = """你是飞书代码自动化助手的意图解析器。根据用户消息与会话上下文，输出严格 JSON（不要 markdown）：
{
  "action": "clarify|confirm_plan|execute|iterate|cancel|chitchat",
  "repo": "owner/repo 或短名",
  "base_branch": "用户明确指定的基线分支，未指定则留空",
  "work_branch_hint": "用户指定的工作分支名，可空",
  "prompt": "给 Claude Code 的完整执行说明",
  "reply_to_user": "给用户的中文回复",
  "missing_fields": ["repo"|"base_branch"|"prompt" 等缺失项],
  "confidence": 0.0-1.0
}

规则：
1. 新需求缺仓库、基线分支或具体改动描述时用 clarify，并在 reply_to_user 追问。
2. 信息齐全、尚未确认执行时用 confirm_plan，reply 中简述计划。
3. 用户明确确认（确认/执行/开始吧）且计划齐全时用 execute。
4. 已有 PR/工作分支后，用户要求继续改用 iterate，prompt 写本轮增量。
5. 用户取消用 cancel；闲聊/问能力用 chitchat。
6. 仅使用 allowed_repos 中的仓库；用户说短名时填短名或完整名均可。
7. base_branch 必须来自用户明确指定（如「基于 dev_test」「从 main 开分支」）；不要默认填 main 或其他分支。未指定时 missing_fields 加入 base_branch 并追问。
8. 不要编造未提供的需求细节。
"""


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        raise ValueError("LLM response is not valid JSON")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM JSON root must be an object")
    return data


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def interpret(
        self,
        *,
        user_text: str,
        session: ConversationSession,
        allowed_repos: list[str],
        default_base_branch: str,
    ) -> IntentResult:
        if self.settings.dry_run or not self.settings.orch_llm_api_key:
            return self._mock_intent(user_text=user_text, session=session, allowed_repos=allowed_repos)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "system",
                "content": json.dumps(
                    {
                        "allowed_repos": allowed_repos,
                "default_base_branch": "",
                "note": "base_branch must be explicitly provided by the user; do not invent a default",
                "session": {
                            "status": session.status.value,
                            "repo": session.repo,
                            "base_branch": session.base_branch,
                            "work_branch": session.work_branch,
                            "prompt": session.prompt,
                            "pr_url": session.pr_url,
                            "current_task_id": session.current_task_id,
                        },
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        for item in session.messages[-12:]:
            role = "assistant" if item.role == "assistant" else "user"
            messages.append({"role": role, "content": item.content})
        messages.append({"role": "user", "content": user_text})

        payload = {
            "model": self.settings.orch_llm_model,
            # kimi-for-coding only accepts temperature=1
            "temperature": 1,
            "messages": messages,
        }
        raw = self._chat_completions(payload)
        content = self._extract_content(raw)
        return IntentResult.from_dict(extract_json_object(content))

    def _chat_completions(self, payload: dict[str, Any]) -> dict[str, Any]:
        base = self.settings.orch_llm_base_url.rstrip("/")
        url = f"{base}/v1/chat/completions" if not base.endswith("/v1") else f"{base}/chat/completions"
        if "/chat/completions" not in url:
            url = f"{base}/chat/completions"

        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.orch_llm_api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM API error {exc.code}: {detail}") from exc

    @staticmethod
    def _extract_content(raw: dict[str, Any]) -> str:
        choices = raw.get("choices") or []
        if not choices:
            raise RuntimeError("LLM response missing choices")
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    parts.append(str(item))
            content = "".join(parts)
        if not content:
            raise RuntimeError("LLM response missing content")
        return str(content)

    def _mock_intent(
        self,
        *,
        user_text: str,
        session: ConversationSession,
        allowed_repos: list[str],
    ) -> IntentResult:
        text = user_text.strip()
        lowered = text.lower()

        if any(word in text for word in ("取消", "算了", "停止")) or "cancel" in lowered:
            return IntentResult(action="cancel", reply_to_user="好的，已取消当前会话。", confidence=1.0)

        if session.status.value == "awaiting_confirm" and any(
            word in text for word in ("确认", "执行", "开始", "可以", "没问题")
        ):
            return IntentResult(
                action="execute",
                repo=session.repo,
                base_branch=session.base_branch,
                work_branch_hint=session.work_branch,
                prompt=session.prompt,
                reply_to_user="好的，开始调度 Claude Code 执行。",
                confidence=1.0,
            )

        if session.status.value == "awaiting_feedback" and session.work_branch:
            return IntentResult(
                action="iterate",
                repo=session.repo,
                base_branch=session.base_branch,
                work_branch_hint=session.work_branch,
                prompt=text,
                reply_to_user="收到，将在同一分支上继续修改。",
                confidence=0.9,
            )

        if session.status.value == "awaiting_approval" and any(word in text for word in ("批准", "同意", "approve")):
            return IntentResult(
                action="execute",
                repo=session.repo,
                base_branch=session.base_branch,
                work_branch_hint=session.work_branch,
                prompt=session.prompt,
                reply_to_user="已记录批准，开始执行。",
                confidence=1.0,
            )

        repo = session.repo
        for allowed in allowed_repos:
            short = allowed.split("/")[-1].lower()
            if short and short in lowered:
                repo = allowed
                break
            if allowed.lower() in lowered:
                repo = allowed
                break

        work_hint = session.work_branch
        base_branch = session.base_branch
        base_match = re.search(
            r"(?:基于|从|base(?:\s*branch)?\s*[:=]?)\s*[「\"']?([A-Za-z0-9._/-]+)[」\"']?",
            text,
            re.IGNORECASE,
        )
        if base_match:
            base_branch = base_match.group(1)

        branch_match = re.search(
            r"(?:分支|branch)\s*[「\"']?([A-Za-z0-9._/-]+)[」\"']?",
            text,
            re.IGNORECASE,
        )
        if branch_match:
            work_hint = branch_match.group(1)
        else:
            create_match = re.search(
                r"创建.*?([A-Za-z][A-Za-z0-9._/-]*)\s*分支",
                text,
            )
            if create_match:
                work_hint = create_match.group(1)
            else:
                named_branch = re.search(
                    r"([A-Za-z][A-Za-z0-9._/-]*)\s*分支",
                    text,
                )
                if named_branch:
                    candidate = named_branch.group(1)
                    if candidate != base_branch:
                        work_hint = candidate

        prompt = session.prompt
        if "功能" in text or "修复" in text or "新增" in text or "fix" in lowered or len(text) > 15:
            prompt = text if not prompt else f"{prompt}\n补充：{text}"

        missing: list[str] = []
        if not repo:
            missing.append("repo")
        if not base_branch:
            missing.append("base_branch")
        if not prompt:
            missing.append("prompt")

        if missing:
            ask = []
            if "repo" in missing:
                ask.append(f"请指定仓库（可选：{', '.join(allowed_repos) or 'owner/repo'}）")
            if "base_branch" in missing:
                ask.append("请指定基于哪个已有分支开发（例如 dev_test）")
            if "prompt" in missing:
                ask.append("请描述要做的具体改动")
            return IntentResult(
                action="clarify",
                repo=repo,
                base_branch=base_branch,
                work_branch_hint=work_hint,
                prompt=prompt,
                reply_to_user="；".join(ask) + "。",
                missing_fields=missing,
                confidence=0.7,
            )

        return IntentResult(
            action="confirm_plan",
            repo=repo,
            base_branch=base_branch,
            work_branch_hint=work_hint,
            prompt=prompt,
            reply_to_user="计划已整理，请确认后开始执行。",
            confidence=0.85,
        )
