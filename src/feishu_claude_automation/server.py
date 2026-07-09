from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .config import Settings
from .orchestrator import Orchestrator
from .policy import PolicyError


class AutomationHandler(BaseHTTPRequestHandler):
    orchestrator: Orchestrator

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

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
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
        try:
            payload = self._read_json()
            if path == "/feishu/events":
                result = self.orchestrator.handle_feishu_message(payload)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/feishu/actions":
                result = self.orchestrator.handle_card_action(payload)
                self._send_json(HTTPStatus.OK, result)
                return
            if path == "/callbacks/runner":
                task = self.orchestrator.handle_runner_callback(payload)
                self._send_json(HTTPStatus.OK, {"task_id": task.id, "status": task.status.value})
                return
            if path == "/tasks":
                from .models import TaskRequest

                request = TaskRequest(
                    repo=payload["repo"],
                    prompt=payload["prompt"],
                    base_branch=payload.get("base_branch", "main"),
                    requester_id=payload.get("requester_id", ""),
                    chat_id=payload.get("chat_id", ""),
                    issue=payload.get("issue", ""),
                )
                task = self.orchestrator.create_task(request)
                self._send_json(HTTPStatus.CREATED, task.to_dict())
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except PolicyError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except KeyError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"missing field: {exc.args[0]}"})
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})


def create_server(settings: Settings | None = None) -> ThreadingHTTPServer:
    settings = settings or Settings.from_env()
    orchestrator = Orchestrator(settings)
    handler = type("ConfiguredAutomationHandler", (AutomationHandler,), {})
    handler.orchestrator = orchestrator
    return ThreadingHTTPServer((settings.host, settings.port), handler)


def main() -> None:
    settings = Settings.from_env()
    server = create_server(settings)
    print(f"feishu-claude orchestrator listening on {settings.host}:{settings.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
