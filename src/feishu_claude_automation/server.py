from __future__ import annotations

import json
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_platform.app import PlatformApp, build_platform_app
from agent_platform.errors import PlatformError, StaleLeaseError

from .config import Settings
from .orchestrator import Orchestrator
from .policy import PolicyError


class AutomationHandler(BaseHTTPRequestHandler):
    orchestrator: Orchestrator
    platform: PlatformApp | None = None

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _log_request(
        self,
        path: str,
        payload: dict[str, Any],
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        log_path = Path(self.orchestrator.settings.audit_log_path).parent / "requests.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "path": path,
            "client": self.client_address[0],
            "payload_keys": sorted(payload.keys()),
            "event_type": payload.get("header", {}).get("event_type") or payload.get("type"),
            "result": result,
            "error": error,
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def _worker_authorized(self) -> bool:
        expected = (self.orchestrator.settings.local_worker_token or "").strip()
        if not expected:
            return False
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return auth[7:].strip() == expected

    def _register_feishu_inbox(
        self,
        payload: dict[str, Any],
        *,
        kind: str,
    ) -> dict[str, Any] | None:
        """Register a Feishu delivery before invoking the compatibility orchestrator."""
        if self.platform is None or payload.get("type") == "url_verification":
            return None
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
        action = event.get("action") if isinstance(event.get("action"), dict) else {}
        context = action.get("context") if isinstance(action.get("context"), dict) else {}
        event_context = event.get("context") if isinstance(event.get("context"), dict) else {}
        event_id = str(header.get("event_id") or payload.get("event_id") or "")
        message_id = str(
            message.get("message_id")
            or context.get("open_message_id")
            or event_context.get("open_message_id")
            or action.get("action_id")
            or ""
        )
        chat_id = str(
            message.get("chat_id")
            or context.get("open_chat_id")
            or event_context.get("open_chat_id")
            or ""
        )
        if not (event_id or message_id) or not chat_id:
            return None
        return self.platform.store.register_inbound_message(
            tenant_key=str(header.get("tenant_key") or payload.get("tenant_key") or "default"),
            chat_id=chat_id,
            event_id=event_id,
            message_id=message_id,
            requester_id=str(sender_id.get("open_id") or ""),
            payload=payload,
            kind=kind,
        )

    def _handle_feishu_idempotently(
        self,
        payload: dict[str, Any],
        *,
        kind: str,
    ) -> dict[str, Any]:
        """Execute a Feishu delivery once and replay its persisted result."""
        inbox = self._register_feishu_inbox(payload, kind=kind)
        if inbox and inbox["duplicate"] and inbox["status"] == "processed":
            return {**inbox["result"], "idempotent_replay": True}
        if inbox and not self.platform.store.claim_inbound_message(inbox["message_row_id"]):
            return {
                "task_id": inbox["task_id"],
                "status": inbox["status"],
                "idempotent_replay": True,
            }
        try:
            if kind == "action":
                result = self.orchestrator.handle_card_action(payload)
            else:
                result = self.orchestrator.handle_feishu_message(payload)
            if inbox:
                legacy_run_id = str(result.get("task_id") or "")
                result = {
                    **result,
                    "task_id": inbox["task_id"],
                    "run_id": legacy_run_id or result.get("run_id", ""),
                }
                self.platform.store.complete_inbound_message(inbox["message_row_id"], result)
            return result
        except Exception as exc:
            if inbox:
                self.platform.store.fail_inbound_message(inbox["message_row_id"], str(exc))
            raise

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            payload: dict[str, Any] = {"status": "ok"}
            if self.platform is not None:
                payload.update(self.platform.health())
            self._send_json(HTTPStatus.OK, payload)
            return
        if path.startswith("/v1/jobs/"):
            if self.platform is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "platform not enabled"})
                return
            job_id = path.split("/v1/jobs/", 1)[1].strip("/")
            try:
                result = self.platform.orchestra.get_job_bundle(job_id)
                self._send_json(HTTPStatus.OK, result)
            except PlatformError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            return
        if path.startswith("/tasks/"):
            task_id = path.split("/tasks/", 1)[1]
            task = self.orchestrator.get_task(task_id)
            if not task:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
                return
            self._send_json(HTTPStatus.OK, task.to_dict())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        payload: dict[str, Any] = {}
        try:
            payload = self._read_json()
            if path == "/feishu/events":
                result = self._handle_feishu_idempotently(payload, kind="message")
                self._log_request(path, payload, result=result)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/feishu/actions":
                result = self._handle_feishu_idempotently(payload, kind="action")
                self._log_request(path, payload, result=result)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/callbacks/runner":
                if payload.get("lease_token"):
                    # Fencing validation prevents a superseded worker from changing user-visible state.
                    self.orchestrator.executor.local_worker.heartbeat(
                        payload,
                        phase=str(payload.get("phase") or ""),
                    )
                if self.platform is not None:
                    try:
                        task = self.platform.bus.apply_callback(payload)
                        result = {
                            "task_id": task.job_id,
                            "run_id": task.id,
                            "job_id": task.id,
                            "status": task.status.value,
                            "via": "platform",
                        }
                        self._log_request(path, payload, result=result)
                        self._send_json(HTTPStatus.OK, result)
                        return
                    except PlatformError:
                        pass
                task = self.orchestrator.handle_runner_callback(payload)
                result = {
                    "task_id": task.session_id or task.id,
                    "run_id": task.id,
                    "job_id": task.id,
                    "status": task.status.value,
                    "via": "legacy",
                }
                self._log_request(path, payload, result=result)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/v1/jobs":
                if self.platform is None:
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "platform not enabled"})
                    return
                result = self.platform.orchestra.create_and_dispatch(
                    goal=str(payload.get("goal") or ""),
                    requester_id=str(payload.get("requester_id") or ""),
                    chat_id=str(payload.get("chat_id") or ""),
                    agent_id=str(payload.get("agent_id") or ""),
                    inputs=payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {},
                    auto_dispatch=bool(payload.get("auto_dispatch", True)),
                    tenant_key=str(payload.get("tenant_key") or "default"),
                    command_key=str(
                        payload.get("command_key")
                        or payload.get("idempotency_key")
                        or payload.get("message_id")
                        or ""
                    ),
                )
                self._log_request(path, payload, result={"job_id": result["job"]["id"]})
                self._send_json(HTTPStatus.CREATED, result)
                return
            if path == "/v1/tasks/callback":
                if self.platform is None:
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "platform not enabled"})
                    return
                task = self.platform.bus.apply_callback(payload)
                result = {
                    "task_id": task.job_id,
                    "run_id": task.id,
                    "job_id": task.id,
                    "status": task.status.value,
                }
                self._log_request(path, payload, result=result)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/v1/worker/jobs":
                if not self._worker_authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                from .models import Task, TaskMode, TaskStatus, RiskLevel

                task = Task(
                    id=str(payload.get("run_id") or payload.get("job_id") or payload.get("id") or ""),
                    repo=str(payload.get("repo") or ""),
                    prompt=str(payload.get("prompt") or ""),
                    base_branch=str(payload.get("base_branch") or ""),
                    work_branch=str(payload.get("work_branch") or ""),
                    requester_id=str(payload.get("requester_id") or ""),
                    chat_id=str(payload.get("chat_id") or ""),
                    status=TaskStatus.QUEUED,
                    risk_level=RiskLevel.LOW,
                    mode=TaskMode(str(payload.get("mode") or TaskMode.CREATE.value)),
                    executor=str(payload.get("executor") or "local_worker"),
                    delivery=str(payload.get("delivery") or "push"),
                    model=str(payload.get("model") or ""),
                    session_id=str(payload.get("task_id") or ""),
                )
                if not task.id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "run_id/job_id is required"})
                    return
                local_path = self.orchestrator.policy.local_path_for(task.repo)
                if not local_path:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": f"no local_path configured for repo: {task.repo}"},
                    )
                    return
                result = self.orchestrator.executor.local_worker.enqueue(
                    task,
                    local_path=local_path,
                    provider=self.orchestrator.policy.provider_for(task.repo),
                )
                self._log_request(path, payload, result=result)
                self._send_json(HTTPStatus.CREATED, result)
                return
            if path == "/v1/worker/jobs/claim":
                if not self._worker_authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                job = self.orchestrator.executor.local_worker.claim(
                    worker_id=str(payload.get("worker_id") or "")
                )
                result = {"job": job}
                self._log_request(path, payload, result={"run_id": job.get("run_id") if job else None})
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/v1/worker/jobs/heartbeat":
                if not self._worker_authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                result = self.orchestrator.executor.local_worker.heartbeat(
                    payload,
                    phase=str(payload.get("phase") or ""),
                )
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/v1/worker/jobs/events":
                if not self._worker_authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                result = self.orchestrator.executor.local_worker.event(
                    payload,
                    sequence=int(payload.get("sequence") or 0),
                    event_type=str(payload.get("event_type") or "progress"),
                    phase=str(payload.get("phase") or ""),
                    payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                )
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/v1/worker/jobs/cancel":
                if not self._worker_authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                run_id = str(payload.get("run_id") or payload.get("job_id") or "")
                if not run_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "run_id/job_id is required"})
                    return
                result = self.orchestrator.executor.local_worker.cancel(
                    run_id,
                    reason=str(payload.get("reason") or "cancelled by user"),
                )
                self._send_json(HTTPStatus.OK, result or {"run_id": run_id, "status": "not_found"})
                return
            if path == "/v1/worker/jobs/complete":
                if not self._worker_authorized():
                    self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                run_id = str(payload.get("run_id") or payload.get("job_id") or "")
                if not run_id:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "run_id/job_id is required"})
                    return
                status = str(payload.get("status") or "completed")
                completed = self.orchestrator.executor.local_worker.complete(
                    {**payload, "run_id": run_id},
                    status=status,
                    result=payload.get("result") if isinstance(payload.get("result"), dict) else {},
                    error=str(payload.get("error") or ""),
                )
                result = completed or {"run_id": run_id, "job_id": run_id, "status": status}
                self._log_request(path, payload, result=result)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/tasks":
                from .models import TaskRequest

                request = TaskRequest(
                    repo=payload["repo"],
                    prompt=payload["prompt"],
                    base_branch=payload.get("base_branch", ""),
                    requester_id=payload.get("requester_id", ""),
                    chat_id=payload.get("chat_id", ""),
                    issue=payload.get("issue", ""),
                    executor=payload.get("executor", ""),
                    delivery=payload.get("delivery", ""),
                )
                task = self.orchestrator.create_task(request)
                result = task.to_dict()
                self._log_request(path, payload, result={"task_id": task.id, "status": task.status.value})
                self._send_json(HTTPStatus.CREATED, result)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except PolicyError as exc:
            self._log_request(path, payload, error=str(exc))
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except StaleLeaseError as exc:
            self._log_request(path, payload, error=str(exc))
            self._send_json(HTTPStatus.CONFLICT, {"error": str(exc), "code": "stale_lease"})
        except PlatformError as exc:
            self._log_request(path, payload, error=str(exc))
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except KeyError as exc:
            self._log_request(path, payload, error=f"missing field: {exc.args[0]}")
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"missing field: {exc.args[0]}"})
        except json.JSONDecodeError:
            self._log_request(path, payload, error="invalid json")
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
        except Exception as exc:  # noqa: BLE001
            self._log_request(path, payload, error=str(exc))
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


def create_server(settings: Settings | None = None, *, enable_platform: bool = True) -> ThreadingHTTPServer:
    settings = settings or Settings.from_env()
    orchestrator = Orchestrator(settings)
    platform_app: PlatformApp | None = None
    if enable_platform:
        # PostgreSQL is the production source of truth; startup must fail instead of
        # silently falling back to the legacy JSON stores.
        platform_app = build_platform_app(settings)
    handler = type("ConfiguredAutomationHandler", (AutomationHandler,), {})
    handler.orchestrator = orchestrator
    handler.platform = platform_app
    return ThreadingHTTPServer((settings.host, settings.port), handler)


def main() -> None:
    settings = Settings.from_env()
    server = create_server(settings)
    print(f"feishu-claude orchestrator listening on {settings.host}:{settings.port}")
    if getattr(server.RequestHandlerClass, "platform", None) is not None:
        print("platform API enabled: POST /v1/jobs, GET /v1/jobs/{id}")
    server.serve_forever()


if __name__ == "__main__":
    main()
