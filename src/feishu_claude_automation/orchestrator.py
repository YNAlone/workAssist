from __future__ import annotations

from uuid import uuid4

from .audit import AuditLogger
from .cards import build_task_card
from .config import Settings
from .feishu import FeishuClient
from .github import GitHubClient
from .models import Task, TaskRequest, TaskStatus, utc_now
from .policy import Policy, PolicyError
from .store import TaskStore


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.policy = Policy.load(settings.policy_file)
        self.store = TaskStore(settings.task_store_path)
        self.audit = AuditLogger(settings.audit_log_path)
        self.feishu = FeishuClient(settings)
        self.github = GitHubClient(settings)

    def _append_audit(self, task: Task, event: str, **payload: object) -> None:
        task.audit.append({"timestamp": utc_now(), "event": event, **payload})
        self.audit.record(event, task_id=task.id, **payload)

    def _notify(self, task: Task) -> None:
        self.feishu.notify_task(task, build_task_card(task))

    def _active_job_count(self) -> int:
        return len(self.store.list_active())

    def create_task(self, request: TaskRequest) -> Task:
        self.policy.validate_request(request)
        if self._active_job_count() >= self.policy.max_concurrent_jobs:
            raise PolicyError("Too many active jobs")

        risk_level = self.policy.classify_risk(request.prompt)
        task_id = uuid4().hex[:12]
        work_branch = self.policy.build_work_branch(task_id)
        self.policy.ensure_work_branch_allowed(work_branch)

        task = Task.from_request(request, work_branch=work_branch, risk_level=risk_level)
        task.id = task_id
        self._append_audit(task, "task.received", repo=task.repo, risk=task.risk_level.value)

        if self.policy.requires_approval(task.risk_level):
            task.status = TaskStatus.PENDING_APPROVAL
            task = self.store.save(task)
            self._append_audit(task, "task.pending_approval")
            self._notify(task)
            return task

        return self.dispatch_task(task)

    def dispatch_task(self, task: Task) -> Task:
        if task.status == TaskStatus.CANCELLED:
            raise PolicyError("Task already cancelled")

        task.status = TaskStatus.QUEUED
        task = self.store.save(task)
        self._append_audit(task, "task.queued")

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
        )
        return self.create_task(request)

    def handle_runner_callback(self, payload: dict) -> Task:
        task_id = payload.get("job_id", "")
        task = self._require_task(task_id)
        status = payload.get("status", "")
        task.summary = payload.get("summary", task.summary)
        task.pr_url = payload.get("pr_url", task.pr_url)
        task.commit_sha = payload.get("commit_sha", task.commit_sha)
        task.error = payload.get("error", "")

        if status == "running":
            task.status = TaskStatus.RUNNING
        elif status == "pr_created":
            task.status = TaskStatus.PR_CREATED
        elif status == "failed":
            task.status = TaskStatus.FAILED
        task = self.store.save(task)
        self._append_audit(task, "runner.callback", status=status)
        self._notify(task)
        return task

    def handle_feishu_message(self, payload: dict) -> dict:
        if not self.feishu.verify_token(payload.get("token", "")):
            raise PolicyError("Invalid Feishu verification token")

        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge", "")}

        event = payload.get("header", {}).get("event_type") or payload.get("event", {}).get("type")
        if event not in {"im.message.receive_v1", "message"}:
            return {"ignored": True, "event": event}

        from .parser import extract_message_text, parse_command

        text = extract_message_text(payload)
        request = parse_command(text)
        if not request:
            return {"ignored": True, "reason": "unsupported command"}

        event_body = payload.get("event", {})
        sender = event_body.get("sender", {}).get("sender_id", {})
        request.requester_id = sender.get("open_id") or sender.get("user_id") or request.requester_id
        request.chat_id = event_body.get("message", {}).get("chat_id", request.chat_id)
        request.message_id = event_body.get("message", {}).get("message_id", request.message_id)

        task = self.create_task(request)
        return {"task_id": task.id, "status": task.status.value}

    def handle_card_action(self, payload: dict) -> dict:
        if not self.feishu.verify_token(payload.get("token", "")):
            raise PolicyError("Invalid Feishu verification token")

        action = payload.get("action", {})
        value = action.get("value", {})
        action_name = value.get("action")
        task_id = value.get("task_id")
        operator = action.get("operator", {}).get("open_id", "")

        if action_name == "approve":
            task = self.approve_task(task_id, operator)
        elif action_name == "cancel":
            task = self.cancel_task(task_id, operator)
        elif action_name == "rerun":
            task = self.rerun_task(task_id)
        else:
            return {"ignored": True, "action": action_name}

        return {"task_id": task.id, "status": task.status.value}

    def _require_task(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if not task:
            raise PolicyError(f"Task not found: {task_id}")
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self.store.get(task_id)
