from __future__ import annotations

import json
from uuid import uuid4

from .audit import AuditLogger
from .cards import build_confirm_plan_card, build_task_card
from .config import Settings
from .feishu import FeishuClient
from .feishu_docs import FeishuDocService
from .github import GitHubClient
from .llm import IntentResult, LLMClient
from .models import (
    ConversationSession,
    SessionStatus,
    Task,
    TaskMode,
    TaskRequest,
    TaskStatus,
    utc_now,
)
from .parser import (
    extract_message_text,
    looks_like_cancel,
    looks_like_confirmation,
    parse_command,
    strip_feishu_mentions,
)
from .policy import Policy, PolicyError
from .session_store import SessionStore
from .store import TaskStore


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.policy = Policy.load(settings.policy_file)
        self.store = TaskStore(settings.task_store_path)
        self.sessions = SessionStore(settings.session_store_path, settings.session_ttl_minutes)
        self.audit = AuditLogger(settings.audit_log_path)
        self.feishu = FeishuClient(settings)
        self.github = GitHubClient(settings)
        self.llm = LLMClient(settings)

    def _append_audit(self, task: Task, event: str, **payload: object) -> None:
        task.audit.append({"timestamp": utc_now(), "event": event, **payload})
        self.audit.record(event, task_id=task.id, **payload)

    def _notify(self, task: Task) -> None:
        try:
            self.feishu.notify_task(task, build_task_card(task))
        except Exception as exc:  # noqa: BLE001 - notification must not fail the main flow
            self._append_audit(task, "feishu.notify_failed", error=str(exc))
            self.store.save(task)

    def _reply_text(self, chat_id: str, text: str, message_id: str = "") -> None:
        if not chat_id or not text:
            return
        try:
            self.feishu.send_text(chat_id, text, reply_to_message_id=message_id)
        except Exception as exc:  # noqa: BLE001
            self.audit.record("feishu.text_failed", error=str(exc), chat_id=chat_id)

    def _active_job_count(self) -> int:
        return len(self.store.list_active())

    def create_task(self, request: TaskRequest) -> Task:
        if request.repo:
            request.repo = self.policy.resolve_repo(request.repo) or request.repo
        request.base_branch = (request.base_branch or "").strip()
        self.policy.validate_request(request)
        if self._active_job_count() >= self.policy.max_concurrent_jobs:
            raise PolicyError("Too many active jobs")

        risk_level = self.policy.classify_risk(request.prompt)
        task_id = uuid4().hex[:12]
        if request.mode == TaskMode.ITERATE and request.work_branch:
            work_branch = request.work_branch
            self.policy.ensure_work_branch_allowed(work_branch)
        elif request.mode == TaskMode.CREATE:
            # Always allocate a fresh work branch for create tasks.
            # Reusing session.work_branch (e.g. ai/dev-<old>) or the base_branch
            # causes "No commits between base and head" / checkout -b failures.
            hint = (request.work_branch or "").strip()
            prefix = f"{self.policy.work_branch_prefix}-"
            if hint and hint != request.base_branch and not hint.startswith(prefix):
                work_branch = self.policy.normalize_work_branch(hint, task_id)
                if work_branch == request.base_branch:
                    work_branch = self.policy.build_work_branch(task_id)
            else:
                work_branch = self.policy.build_work_branch(task_id)
            self.policy.ensure_work_branch_allowed(work_branch)
        elif request.work_branch:
            work_branch = self.policy.normalize_work_branch(request.work_branch, task_id)
            if work_branch == request.base_branch:
                work_branch = self.policy.build_work_branch(task_id)
        else:
            work_branch = self.policy.build_work_branch(task_id)
            self.policy.ensure_work_branch_allowed(work_branch)

        task = Task.from_request(request, work_branch=work_branch, risk_level=risk_level)
        task.id = task_id
        self._append_audit(
            task,
            "task.received",
            repo=task.repo,
            risk=task.risk_level.value,
            mode=task.mode.value,
        )

        if self.policy.requires_approval(task.risk_level):
            task.status = TaskStatus.PENDING_APPROVAL
            task = self.store.save(task)
            self._append_audit(task, "task.pending_approval")
            self._sync_session_from_task(task, SessionStatus.AWAITING_APPROVAL)
            self._notify(task)
            return task

        return self.dispatch_task(task)

    def dispatch_task(self, task: Task) -> Task:
        if task.status == TaskStatus.CANCELLED:
            raise PolicyError("Task already cancelled")

        task.status = TaskStatus.QUEUED
        task = self.store.save(task)
        self._append_audit(task, "task.queued")
        self._sync_session_from_task(task, SessionStatus.RUNNING)

        try:
            dispatch_result = self.github.dispatch_workflow(task)
            task.status = TaskStatus.DISPATCHED
            task.dispatch_id = str(dispatch_result.get("job_id", task.id))
            task = self.store.save(task)
            self._append_audit(task, "task.dispatched", dispatch=dispatch_result)
            self._notify(task)
            return task
        except Exception as exc:  # noqa: BLE001 - surface runner dispatch failures to task state
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            task = self.store.save(task)
            self._append_audit(task, "task.failed", error=str(exc))
            self._sync_session_from_task(task, SessionStatus.AWAITING_FEEDBACK)
            self._notify(task)
            raise

    def approve_task(self, task_id: str, approver_id: str) -> Task:
        task = self._require_task(task_id)
        if task.status != TaskStatus.PENDING_APPROVAL:
            raise PolicyError(f"Task is not pending approval: {task.status.value}")
        task.approved_by = approver_id
        task = self.store.save(task)
        self._append_audit(task, "task.approved", approver=approver_id)
        return self.dispatch_task(task)

    def cancel_task(self, task_id: str, actor_id: str = "") -> Task:
        task = self._require_task(task_id)
        task.status = TaskStatus.CANCELLED
        task = self.store.save(task)
        self._append_audit(task, "task.cancelled", actor=actor_id)
        self._sync_session_from_task(task, SessionStatus.CLOSED)
        self._notify(task)
        return task

    def rerun_task(self, task_id: str) -> Task:
        task = self._require_task(task_id)
        request = TaskRequest(
            repo=task.repo,
            prompt=task.prompt,
            base_branch=task.base_branch,
            requester_id=task.requester_id,
            chat_id=task.chat_id,
            message_id=task.message_id,
            issue=task.issue,
            session_id=task.session_id,
            mode=TaskMode.CREATE,
        )
        return self.create_task(request)

    def handle_runner_callback(self, payload: dict) -> Task:
        task_id = payload.get("job_id", "")
        task = self._require_task(task_id)
        status = payload.get("status", "")
        task.summary = payload.get("summary", task.summary)
        if payload.get("pr_url"):
            task.pr_url = payload.get("pr_url", task.pr_url)
        task.commit_sha = payload.get("commit_sha", task.commit_sha)
        task.error = payload.get("error", "")

        report_markdown = str(payload.get("report_markdown") or "")
        report_url = str(payload.get("report_url") or "")
        report_path = str(payload.get("report_path") or "")
        if report_url and not task.summary:
            task.summary = f"分析报告：{report_url}"
        elif report_path and report_url:
            task.summary = payload.get("summary") or f"分析报告已生成：{report_path}"

        if status == "running":
            task.status = TaskStatus.RUNNING
            session_status = SessionStatus.RUNNING
        elif status in {"pr_created", "updated", "completed", "succeeded"}:
            task.status = TaskStatus.PR_CREATED
            if status in {"completed", "succeeded"} and not payload.get("pr_url"):
                task.summary = payload.get("summary") or task.summary or "Completed without code changes"
            session_status = SessionStatus.AWAITING_FEEDBACK
        elif status == "failed":
            task.status = TaskStatus.FAILED
            session_status = SessionStatus.AWAITING_FEEDBACK
        else:
            session_status = None

        task = self.store.save(task)
        self._append_audit(task, "runner.callback", status=status)
        if session_status is not None:
            self._sync_session_from_task(task, session_status)
        self._notify(task)
        if status in {"pr_created", "updated", "completed", "succeeded"} and (
            report_markdown or report_url
        ):
            self._deliver_report_to_feishu(
                task,
                report_markdown=report_markdown,
                report_url=report_url,
                report_path=report_path,
            )
        return task

    def _deliver_report_to_feishu(
        self,
        task: Task,
        *,
        report_markdown: str,
        report_url: str,
        report_path: str,
    ) -> None:
        """Create a Feishu cloud doc from analysis markdown and reply with the link."""
        if not task.chat_id:
            return

        body = (report_markdown or "").strip()
        feishu_doc_url = ""
        if body:
            title = report_path.rsplit("/", 1)[-1].replace(".md", "") if report_path else f"分析报告-{task.id}"
            try:
                doc_service = FeishuDocService(
                    self.feishu,
                    mount_key=self.settings.feishu_doc_mount_key,
                    mount_folder=self.settings.feishu_doc_mount_folder,
                )
                doc = doc_service.import_markdown(
                    title=title,
                    markdown=body,
                    requester_open_id=task.requester_id,
                    chat_id=task.chat_id,
                )
                feishu_doc_url = doc.url
                task.summary = f"飞书文档：{feishu_doc_url}"
                self.store.save(task)
                self._append_audit(task, "feishu.doc_created", url=feishu_doc_url)
            except Exception as exc:  # noqa: BLE001
                self._append_audit(task, "feishu.doc_failed", error=str(exc))

        limit = 3500
        header_parts = ["## 分析结论"]
        if feishu_doc_url:
            header_parts.append(f"完整文档：[打开飞书云文档]({feishu_doc_url})")
        elif report_url:
            header_parts.append(f"文档链接：[打开]({report_url})")
        if report_path:
            header_parts.append(f"源文件：`{report_path}`")
        if task.pr_url and task.pr_url not in {report_url, feishu_doc_url}:
            header_parts.append(f"PR：[{task.pr_url}]({task.pr_url})")
        header = "\n".join(header_parts) + "\n\n"

        if feishu_doc_url:
            preview_limit = min(800, max(0, limit - len(header) - 20))
            preview = body[:preview_limit].strip()
            suffix = "…" if len(body) > preview_limit else ""
            text = header + (preview + suffix if preview else "分析内容已写入飞书云文档。")
        elif not body:
            text = header + (task.summary or "任务已完成。")
        elif len(header) + len(body) <= limit:
            text = header + body
        else:
            remain = max(0, limit - len(header) - 40)
            text = header + body[:remain] + "\n\n…（内容过长已截断，请打开完整文档查看）"
        self._reply_text(task.chat_id, text, task.message_id)

    def _extract_verification_token(self, payload: dict) -> str:
        # Feishu url_verification uses top-level token; event schema 2.0 uses header.token.
        return str(payload.get("token") or payload.get("header", {}).get("token") or "")

    def handle_feishu_message(self, payload: dict) -> dict:
        challenge = self._handle_url_verification(payload)
        if challenge is not None:
            return challenge

        if not self.feishu.verify_token(self._extract_verification_token(payload)):
            raise PolicyError("Invalid Feishu verification token")

        event = payload.get("header", {}).get("event_type") or payload.get("event", {}).get("type")
        if event not in {"im.message.receive_v1", "message"}:
            self.audit.record("feishu.ignored", reason="unsupported_event", event=event)
            return {"ignored": True, "event": event}

        text = extract_message_text(payload)
        event_body = payload.get("event", {})
        sender = event_body.get("sender", {}).get("sender_id", {})
        requester_id = sender.get("open_id") or sender.get("user_id") or ""
        chat_id = event_body.get("message", {}).get("chat_id", "")
        message_id = event_body.get("message", {}).get("message_id", "")

        request = parse_command(text)
        if request:
            request.requester_id = requester_id or request.requester_id
            request.chat_id = chat_id or request.chat_id
            request.message_id = message_id or request.message_id
            task = self.create_task(request)
            return {"task_id": task.id, "status": task.status.value, "mode": "command"}

        return self._handle_natural_language(
            text=text,
            requester_id=requester_id,
            chat_id=chat_id,
            message_id=message_id,
        )

    def _handle_natural_language(
        self,
        *,
        text: str,
        requester_id: str,
        chat_id: str,
        message_id: str,
    ) -> dict:
        cleaned = strip_feishu_mentions(text)
        if not cleaned:
            return {"ignored": True, "reason": "empty message"}

        session = self.sessions.get_active(chat_id, requester_id)
        if not session:
            session = ConversationSession.create(chat_id=chat_id, requester_id=requester_id)
            session.status = SessionStatus.CLARIFYING
            self.sessions.save(session)

        if session.status == SessionStatus.RUNNING and not looks_like_cancel(cleaned):
            reply = "当前任务仍在执行中，请稍候。完成后可继续回复修改需求。"
            self._reply_text(chat_id, reply, message_id)
            return {"session_id": session.id, "status": session.status.value, "replied": True}

        if session.status == SessionStatus.AWAITING_APPROVAL and looks_like_confirmation(cleaned):
            if session.current_task_id:
                task = self.approve_task(session.current_task_id, requester_id)
                self._reply_text(chat_id, "已批准，开始执行。", message_id)
                return {"session_id": session.id, "task_id": task.id, "status": task.status.value}

        if session.status == SessionStatus.AWAITING_CONFIRM and looks_like_confirmation(cleaned):
            task = self.confirm_session_execute(session.id, requester_id)
            self._reply_text(chat_id, "已确认，开始执行。", message_id)
            return {
                "session_id": session.id,
                "task_id": task.id,
                "status": task.status.value,
                "action": "execute",
            }

        if looks_like_cancel(cleaned) and session.status != SessionStatus.CLOSED:
            if session.current_task_id:
                task = self.store.get(session.current_task_id)
                if task and task.status in {
                    TaskStatus.PENDING_APPROVAL,
                    TaskStatus.QUEUED,
                    TaskStatus.DISPATCHED,
                    TaskStatus.RUNNING,
                }:
                    self.cancel_task(task.id, requester_id)
            self.sessions.close(session)
            self._reply_text(chat_id, "好的，已取消当前会话。", message_id)
            return {"session_id": session.id, "status": SessionStatus.CLOSED.value}

        try:
            intent = self.llm.interpret(
                user_text=cleaned,
                session=session,
                allowed_repos=self.policy.allowed_repos,
                default_base_branch=self.policy.default_base_branch,
            )
        except Exception as exc:  # noqa: BLE001
            self.audit.record("llm.failed", error=str(exc), session_id=session.id)
            self._reply_text(
                chat_id,
                f"意图解析失败，请稍后重试或改用 /ai-fix 命令。错误：{exc}",
                message_id,
            )
            return {"session_id": session.id, "error": str(exc)}

        self._merge_intent_into_session(session, intent)
        session.append_message("user", cleaned)
        if intent.reply_to_user:
            session.append_message("assistant", intent.reply_to_user)
        self.sessions.save(session)

        return self._apply_intent(
            session=session,
            intent=intent,
            requester_id=requester_id,
            chat_id=chat_id,
            message_id=message_id,
        )

    def _merge_intent_into_session(self, session: ConversationSession, intent: IntentResult) -> None:
        if intent.repo:
            session.repo = self.policy.resolve_repo(intent.repo) or intent.repo
        if intent.base_branch:
            session.base_branch = intent.base_branch.strip()
        if intent.work_branch_hint:
            session.work_branch = intent.work_branch_hint
        if intent.prompt:
            session.prompt = intent.prompt

    def _apply_intent(
        self,
        *,
        session: ConversationSession,
        intent: IntentResult,
        requester_id: str,
        chat_id: str,
        message_id: str,
    ) -> dict:
        action = intent.action

        if action == "cancel":
            self.sessions.close(session)
            self._reply_text(chat_id, intent.reply_to_user or "已取消。", message_id)
            return {"session_id": session.id, "status": SessionStatus.CLOSED.value}

        if action == "chitchat":
            reply = intent.reply_to_user or (
                "我可以帮你用自然语言改代码：请说明仓库、基于哪个已有分支、以及需求；"
                "确认后我会调度 Claude Code。PR 出来后也可继续说「再改一下」。也支持 /ai-fix 命令。"
            )
            self._reply_text(chat_id, reply, message_id)
            session.status = SessionStatus.CLARIFYING if not session.repo else session.status
            self.sessions.save(session)
            return {"session_id": session.id, "status": session.status.value, "action": action}

        if action == "clarify":
            session.status = SessionStatus.CLARIFYING
            self.sessions.save(session)
            self._reply_text(chat_id, intent.reply_to_user or "还需要更多信息才能执行。", message_id)
            return {
                "session_id": session.id,
                "status": session.status.value,
                "action": action,
                "missing_fields": intent.missing_fields,
            }

        if action == "confirm_plan":
            missing = self._missing_plan_fields(session)
            if missing:
                session.status = SessionStatus.CLARIFYING
                self.sessions.save(session)
                self._reply_text(
                    chat_id,
                    intent.reply_to_user or f"还缺：{', '.join(missing)}",
                    message_id,
                )
                return {"session_id": session.id, "status": session.status.value, "missing_fields": missing}

            session.status = SessionStatus.AWAITING_CONFIRM
            self.sessions.save(session)
            if intent.reply_to_user:
                self._reply_text(chat_id, intent.reply_to_user, message_id)
            try:
                self.feishu.send_card(chat_id, build_confirm_plan_card(session))
            except Exception as exc:  # noqa: BLE001
                self.audit.record("feishu.card_failed", error=str(exc), session_id=session.id)
            return {"session_id": session.id, "status": session.status.value, "action": action}

        if action == "execute":
            if session.status == SessionStatus.AWAITING_CONFIRM or looks_like_confirmation(intent.reply_to_user):
                pass
            missing = self._missing_plan_fields(session)
            if missing:
                session.status = SessionStatus.CLARIFYING
                self.sessions.save(session)
                self._reply_text(chat_id, f"还不能执行，缺少：{', '.join(missing)}", message_id)
                return {"session_id": session.id, "missing_fields": missing}
            if intent.reply_to_user:
                self._reply_text(chat_id, intent.reply_to_user, message_id)
            task = self._create_task_from_session(session, requester_id=requester_id, message_id=message_id)
            return {
                "session_id": session.id,
                "task_id": task.id,
                "status": task.status.value,
                "action": action,
            }

        if action == "iterate":
            return self._start_iteration(
                session=session,
                prompt=intent.prompt or strip_feishu_mentions(intent.reply_to_user),
                requester_id=requester_id,
                chat_id=chat_id,
                message_id=message_id,
                reply=intent.reply_to_user,
            )

        # Fallback: treat as clarify
        session.status = SessionStatus.CLARIFYING
        self.sessions.save(session)
        self._reply_text(chat_id, intent.reply_to_user or "请继续补充需求。", message_id)
        return {"session_id": session.id, "status": session.status.value, "action": "clarify"}

    def _missing_plan_fields(self, session: ConversationSession) -> list[str]:
        missing: list[str] = []
        if not session.repo:
            missing.append("repo")
        if not session.base_branch:
            missing.append("base_branch")
        if not session.prompt:
            missing.append("prompt")
        return missing

    def _create_task_from_session(
        self,
        session: ConversationSession,
        *,
        requester_id: str,
        message_id: str,
        mode: TaskMode = TaskMode.CREATE,
        parent_task_id: str = "",
        iteration: int = 0,
        prompt: str | None = None,
    ) -> Task:
        request = TaskRequest(
            repo=session.repo,
            prompt=prompt or session.prompt,
            base_branch=session.base_branch,
            requester_id=requester_id or session.requester_id,
            chat_id=session.chat_id,
            message_id=message_id,
            work_branch=session.work_branch,
            session_id=session.id,
            mode=mode,
            parent_task_id=parent_task_id,
            iteration=iteration,
        )
        task = self.create_task(request)
        session.current_task_id = task.id
        session.work_branch = task.work_branch
        if task.status == TaskStatus.PENDING_APPROVAL:
            session.status = SessionStatus.AWAITING_APPROVAL
        elif task.status in {TaskStatus.QUEUED, TaskStatus.DISPATCHED, TaskStatus.RUNNING}:
            session.status = SessionStatus.RUNNING
        self.sessions.save(session)
        return task

    def _start_iteration(
        self,
        *,
        session: ConversationSession,
        prompt: str,
        requester_id: str,
        chat_id: str,
        message_id: str,
        reply: str = "",
    ) -> dict:
        latest = self.store.get_latest_by_session(session.id)
        if latest:
            session.work_branch = session.work_branch or latest.work_branch
            session.repo = session.repo or latest.repo
            session.base_branch = session.base_branch or latest.base_branch
            session.pr_url = session.pr_url or latest.pr_url

        if not session.work_branch or not session.repo:
            self._reply_text(chat_id, "当前没有可迭代的任务，请先发起一次完整需求。", message_id)
            session.status = SessionStatus.CLARIFYING
            self.sessions.save(session)
            return {"session_id": session.id, "error": "no_iterable_task"}

        if not prompt:
            self._reply_text(chat_id, "请说明本轮要继续修改的内容。", message_id)
            return {"session_id": session.id, "error": "missing_prompt"}

        parent_id = session.current_task_id or (latest.id if latest else "")
        iteration = (latest.iteration + 1) if latest else 1
        if reply:
            self._reply_text(chat_id, reply, message_id)
        task = self._create_task_from_session(
            session,
            requester_id=requester_id,
            message_id=message_id,
            mode=TaskMode.ITERATE,
            parent_task_id=parent_id,
            iteration=iteration,
            prompt=prompt,
        )
        return {
            "session_id": session.id,
            "task_id": task.id,
            "status": task.status.value,
            "action": "iterate",
            "mode": TaskMode.ITERATE.value,
        }

    def confirm_session_execute(self, session_id: str, operator_id: str = "") -> Task:
        session = self.sessions.get(session_id)
        if not session or session.status == SessionStatus.CLOSED:
            raise PolicyError(f"Session not found: {session_id}")
        missing = self._missing_plan_fields(session)
        if missing:
            raise PolicyError(f"Session plan incomplete: {', '.join(missing)}")
        return self._create_task_from_session(
            session,
            requester_id=operator_id or session.requester_id,
            message_id="",
        )

    def cancel_session(self, session_id: str, actor_id: str = "") -> ConversationSession:
        session = self.sessions.get(session_id)
        if not session:
            raise PolicyError(f"Session not found: {session_id}")
        if session.current_task_id:
            task = self.store.get(session.current_task_id)
            if task and task.status in {
                TaskStatus.PENDING_APPROVAL,
                TaskStatus.QUEUED,
                TaskStatus.DISPATCHED,
                TaskStatus.RUNNING,
                TaskStatus.RECEIVED,
            }:
                self.cancel_task(task.id, actor_id)
        return self.sessions.close(session)

    def handle_card_action(self, payload: dict) -> dict:
        challenge = self._handle_url_verification(payload)
        if challenge is not None:
            return challenge

        if not self.feishu.verify_token(self._extract_verification_token(payload)):
            raise PolicyError("Invalid Feishu verification token")

        # Feishu card callback schema 2.0 nests action under event.*;
        # older payloads may put action at the top level.
        event_body = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        action = event_body.get("action") if isinstance(event_body.get("action"), dict) else {}
        if not action:
            action = payload.get("action") if isinstance(payload.get("action"), dict) else {}

        raw_value = action.get("value", {})
        if isinstance(raw_value, str):
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError:
                value = {"action": raw_value}
        elif isinstance(raw_value, dict):
            value = raw_value
        else:
            value = {}

        action_name = value.get("action")
        task_id = value.get("task_id")
        session_id = value.get("session_id")
        operator = (
            (event_body.get("operator") or {}).get("open_id")
            or (action.get("operator") or {}).get("open_id")
            or (payload.get("operator") or {}).get("open_id")
            or ""
        )

        if action_name == "approve":
            task = self.approve_task(task_id, operator)
            return {"task_id": task.id, "status": task.status.value}
        if action_name == "cancel":
            task = self.cancel_task(task_id, operator)
            return {"task_id": task.id, "status": task.status.value}
        if action_name == "rerun":
            task = self.rerun_task(task_id)
            return {"task_id": task.id, "status": task.status.value}
        if action_name == "confirm_execute":
            task = self.confirm_session_execute(session_id, operator)
            return {"task_id": task.id, "session_id": session_id, "status": task.status.value}
        if action_name == "cancel_session":
            session = self.cancel_session(session_id, operator)
            self._reply_text(session.chat_id, "已取消会话。")
            return {"session_id": session.id, "status": session.status.value}

        return {"ignored": True, "action": action_name, "value_keys": sorted(value.keys())}

    def _sync_session_from_task(self, task: Task, status: SessionStatus) -> None:
        if not task.session_id:
            return
        session = self.sessions.get(task.session_id)
        if not session or session.status == SessionStatus.CLOSED:
            return
        session.current_task_id = task.id
        session.repo = task.repo
        session.base_branch = task.base_branch
        session.work_branch = task.work_branch
        session.prompt = task.prompt or session.prompt
        if task.pr_url:
            session.pr_url = task.pr_url
        session.status = status
        self.sessions.save(session)

    def _handle_url_verification(self, payload: dict) -> dict | None:
        if payload.get("type") != "url_verification":
            return None
        return {"challenge": payload.get("challenge", "")}

    def _require_task(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if not task:
            raise PolicyError(f"Task not found: {task_id}")
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get(task_id)
