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
    executor: str = ""
    delivery: str = ""

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
            executor=str(data.get("executor", "") or ""),
            delivery=str(data.get("delivery", "") or ""),
        )


SYSTEM_PROMPT = """你是飞书代码自动化助手的意图解析器。根据用户消息与会话上下文，输出严格 JSON（不要 markdown 代码块，不要输出 JSON 以外的正文）：
{
  "action": "clarify|confirm_plan|execute|iterate|cancel|chitchat",
  "repo": "owner/repo 或短名",
  "base_branch": "用户明确指定的基线分支，未指定则留空",
  "work_branch_hint": "用户指定的工作分支名，可空",
  "prompt": "给 Claude Code 的完整执行说明",
  "reply_to_user": "给用户的中文回复（简短）",
  "missing_fields": ["repo"|"base_branch"|"prompt" 等缺失项],
  "confidence": 0.0-1.0,
  "executor": "local_worker|github_actions|gitlab_ci|vcs 或留空",
  "delivery": "push|local_only 或留空"
}

规则：
1. 新需求缺仓库、基线分支或具体改动描述时用 clarify，并在 reply_to_user 追问。
2. 信息齐全、尚未确认执行时用 confirm_plan，reply 中简述计划。
3. 用户明确确认（确认/执行/开始吧）且计划齐全时用 execute。
4. 已有 PR/工作分支后，用户要求继续改用 iterate，prompt 写本轮增量。
5. 用户取消用 cancel；闲聊/问能力用 chitchat。
6. 仅使用 allowed_repos 中的仓库；用户说短名时填短名或完整名均可。
7. base_branch 必须来自用户明确指定（如用户在会话中说明使用xxx分支/基于xxx分支等）；不要默认填 main 或其他分支。未指定时 missing_fields 加入 base_branch 并追问。
8. 不要编造未提供的需求细节。
9. 用户说「本机跑」「本地 worker」「不用 CI」时 executor=local_worker。
10. 用户说「只改本地」「不要推远程」「不要开 PR」时 delivery=local_only；说「推远程」「开 PR/MR」时 delivery=push。
11. 文档/方案类任务（写文档、开发方案、技术方案、分析报告、AB Test 方案、完整方案、调研报告等，且不要求立刻改业务代码）：
    - 必须当作可派发任务，禁止用 chitchat，也禁止在 reply_to_user 里输出完整长文方案。
    - 缺仓库或基线分支 → action=clarify，missing_fields 补齐后追问。
    - 信息够用 → action=confirm_plan；prompt 写明：在目标仓库基于用户描述（及引用文档要点）产出完整 Markdown 方案，保存为 docs/analysis-*.md，不要只在对话回复；若需代码改动可另说明，但本任务以文档交付为主。
    - reply_to_user 只做一两句计划确认（例如将调度写方案文档）。
"""


DOC_TASK_HINTS = (
    "写文档",
    "开发方案",
    "技术方案",
    "分析报告",
    "方案文档",
    "完整方案",
    "实施方案",
    "设计方案",
    "调研报告",
    "给我一份",
    "产出文档",
    "ab test",
    "abtest",
    "a/b test",
    "a/b测试",
    "ab测试",
)


def looks_like_doc_writing(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    lowered = raw.lower()
    return any(hint in raw or hint in lowered for hint in DOC_TASK_HINTS)


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("LLM response is not valid JSON")

    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence:
        try:
            data = json.loads(fence.group(1))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    # Prefer the first top-level object that looks like our intent schema.
    for match in re.finditer(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", stripped, re.DOTALL):
        candidate = match.group(0)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and ("action" in data or "reply_to_user" in data or "prompt" in data):
            return data

    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        raise ValueError("LLM response is not valid JSON")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError("LLM response is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("LLM JSON root must be an object")
    return data


def resolve_repo_from_text(text: str, allowed_repos: list[str], fallback: str = "") -> str:
    lowered = (text or "").lower()
    for allowed in allowed_repos:
        short = allowed.split("/")[-1].lower()
        if short and short in lowered:
            return allowed
        if allowed.lower() in lowered:
            return allowed
    return fallback


def extract_base_branch_hint(text: str) -> str:
    """Extract an explicit git base branch; ignore 「基于 <文档标题>」这类表述。"""
    raw = text or ""
    named = re.search(
        r"(?:基于|从)\s*[「\"']?([A-Za-z0-9._/-]+)[」\"']?\s*分支",
        raw,
        re.IGNORECASE,
    )
    if named:
        return named.group(1)
    assigned = re.search(
        r"base(?:\s*branch)?\s*[:=]\s*[「\"']?([A-Za-z0-9._/-]+)",
        raw,
        re.IGNORECASE,
    )
    if assigned:
        return assigned.group(1)
    loose = re.search(
        r"(?:基于|从)\s*[「\"']?([A-Za-z0-9._/-]+)[」\"']?",
        raw,
        re.IGNORECASE,
    )
    if not loose:
        return ""
    candidate = loose.group(1)
    rest = raw[loose.end() :]
    # 「基于 CJ报名…」——分支 token 与中文标题粘连
    if rest and "\u4e00" <= rest[0] <= "\u9fff":
        return ""
    # 「基于 xxx 方案/文档」——文档引用，不是分支
    if re.match(r"\s*(?:这个\s*)?(?:文档|方案)", rest):
        return ""
    if candidate.lower() in {"main", "master", "develop", "dev", "release", "dev_test"}:
        return candidate
    if "/" in candidate or "_" in candidate:
        return candidate
    if re.match(r"(?i)(feature|feat|hotfix|bugfix|release)[\w./-]*", candidate):
        return candidate
    return ""


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
            # Kimi Coding models expect temperature=1 for kimi-for-coding; keep 1 for k3 too.
            "temperature": 1,
            "messages": messages,
        }
        raw = self._chat_completions(payload)
        content = self._extract_content(raw)
        try:
            intent = IntentResult.from_dict(extract_json_object(content))
        except (ValueError, json.JSONDecodeError):
            recovered = self._recover_non_json_intent(
                user_text=user_text,
                llm_content=content,
                session=session,
                allowed_repos=allowed_repos,
            )
            if recovered is not None:
                return recovered
            raise
        return self._normalize_doc_writing_intent(
            intent,
            user_text=user_text,
            session=session,
            allowed_repos=allowed_repos,
        )

    def _normalize_doc_writing_intent(
        self,
        intent: IntentResult,
        *,
        user_text: str,
        session: ConversationSession,
        allowed_repos: list[str],
    ) -> IntentResult:
        """If the model treated a doc/plan request as chitchat, upgrade it to a dispatchable plan."""
        if intent.action not in {"chitchat", "clarify"} and intent.prompt:
            if looks_like_doc_writing(user_text) and "docs/analysis" not in intent.prompt:
                intent.prompt = (
                    f"{intent.prompt.rstrip()}\n\n"
                    "（文档/方案任务）请将完整方案写入 `docs/analysis-<task_id>.md`，不要只在对话中回复。"
                )
            return intent
        if not looks_like_doc_writing(user_text):
            return intent
        if intent.action == "clarify" and intent.missing_fields:
            # Keep clarify, but ensure prompt carries the doc-writing instruction for next turn.
            if not intent.prompt:
                intent.prompt = (
                    f"用户需求（文档/方案类任务）：\n{user_text.strip()}\n\n"
                    "请在目标仓库撰写完整 Markdown 方案，保存为 `docs/analysis-<task_id>.md`。"
                )
            return intent

        recovered = self._recover_non_json_intent(
            user_text=user_text,
            llm_content=intent.reply_to_user or intent.prompt,
            session=session,
            allowed_repos=allowed_repos,
        )
        return recovered or intent

    def _recover_non_json_intent(
        self,
        *,
        user_text: str,
        llm_content: str,
        session: ConversationSession,
        allowed_repos: list[str],
    ) -> IntentResult | None:
        """When the model dumps a doc/plan in prose, coerce it into dispatchable intent JSON fields."""
        prose = (llm_content or "").strip()
        if not looks_like_doc_writing(user_text) and not looks_like_doc_writing(prose):
            # Long structured markdown often means the model wrote a plan instead of JSON.
            if not (len(prose) >= 200 and ("#" in prose or "方案" in prose or "实验" in prose)):
                return None

        repo = session.repo or resolve_repo_from_text(user_text, allowed_repos)
        base_branch = (session.base_branch or "").strip() or extract_base_branch_hint(user_text)

        draft = prose[:8000]
        prompt = (
            f"用户需求（文档/方案类任务）：\n{user_text.strip()}\n\n"
            "请在目标仓库撰写完整 Markdown 开发/分析方案，保存为 `docs/analysis-<task_id>.md`"
            "（task_id 由系统注入），不要只在对话中回复。"
            "若用户引用了飞书文档，请结合其目标与仓库现状给出可落地的方案。"
        )
        if draft:
            prompt += f"\n\n---\n意图模型草稿参考（可整理采纳）：\n{draft}"

        missing: list[str] = []
        if not repo:
            missing.append("repo")
        if not base_branch:
            missing.append("base_branch")

        if missing:
            asks: list[str] = []
            if "repo" in missing:
                asks.append(f"请指定仓库（可选：{', '.join(allowed_repos) or 'owner/repo'}）")
            if "base_branch" in missing:
                asks.append("请指定基于哪个已有分支撰写/落库该方案文档（例如 feature_6.3）")
            return IntentResult(
                action="clarify",
                repo=repo,
                base_branch=base_branch,
                work_branch_hint=session.work_branch,
                prompt=prompt,
                reply_to_user="；".join(asks) + "。识别到这是文档/方案类任务，补齐信息后将调度写入仓库。",
                missing_fields=missing,
                confidence=0.75,
                executor=session.executor,
                delivery=session.delivery or "push",
            )

        return IntentResult(
            action="confirm_plan",
            repo=repo,
            base_branch=base_branch,
            work_branch_hint=session.work_branch,
            prompt=prompt,
            reply_to_user=(
                f"已识别为文档/方案任务，将在 `{repo}`（基于 `{base_branch}`）"
                "调度生成 `docs/analysis-*.md` 方案文档，请确认后执行。"
            ),
            confidence=0.8,
            executor=session.executor,
            delivery=session.delivery or "push",
        )

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

        repo = session.repo or resolve_repo_from_text(text, allowed_repos)
        work_hint = session.work_branch
        base_branch = (session.base_branch or "").strip() or extract_base_branch_hint(text)

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

        doc_task = looks_like_doc_writing(text)
        prompt = session.prompt
        if doc_task:
            prompt = (
                f"用户需求（文档/方案类任务）：\n{text}\n\n"
                "请在目标仓库撰写完整 Markdown 方案，保存为 `docs/analysis-<task_id>.md`，"
                "不要只在对话中回复。"
            )
        elif "功能" in text or "修复" in text or "新增" in text or "fix" in lowered or len(text) > 15:
            prompt = text if not prompt else f"{prompt}\n补充：{text}"

        executor = session.executor
        if any(word in text for word in ("本机", "本地 worker", "本地执行", "不用 ci", "不用ci")):
            executor = "local_worker"

        delivery = session.delivery
        if any(word in text for word in ("只改本地", "不要推远程", "不要开 pr", "不要开pr", "不要推送")):
            delivery = "local_only"
        elif any(word in text for word in ("推远程", "开 pr", "开pr", "开 mr", "开mr", "推送远程")):
            delivery = "push"
        elif doc_task and not delivery:
            delivery = "push"

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
                ask.append(
                    "请指定基于哪个已有分支撰写/落库该方案文档（例如 feature_6.3）"
                    if doc_task
                    else "请指定基于哪个已有分支开发（例如 dev_test）"
                )
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
                executor=executor,
                delivery=delivery,
            )

        return IntentResult(
            action="confirm_plan",
            repo=repo,
            base_branch=base_branch,
            work_branch_hint=work_hint,
            prompt=prompt,
            reply_to_user=(
                "已识别为文档/方案任务，将调度生成 `docs/analysis-*.md`，请确认后执行。"
                if doc_task
                else "计划已整理，请确认后开始执行。"
            ),
            confidence=0.85,
            executor=executor,
            delivery=delivery,
        )
