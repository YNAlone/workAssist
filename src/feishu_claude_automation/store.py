from __future__ import annotations

import json
import threading
from pathlib import Path

from .models import Task, TaskStatus, utc_now


class TaskStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def _read_all(self) -> dict[str, dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write_all(self, data: dict[str, dict]) -> None:
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def save(self, task: Task) -> Task:
        with self._lock:
            data = self._read_all()
            task.updated_at = utc_now()
            data[task.id] = task.to_dict()
            self._write_all(data)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            raw = self._read_all().get(task_id)
        return Task.from_dict(raw) if raw else None

    def list_active(self) -> list[Task]:
        active = {
            TaskStatus.QUEUED,
            TaskStatus.DISPATCHED,
            TaskStatus.RUNNING,
            TaskStatus.PENDING_APPROVAL,
        }
        with self._lock:
            tasks = [Task.from_dict(item) for item in self._read_all().values()]
        return [task for task in tasks if task.status in active]

    def get_latest_by_session(self, session_id: str) -> Task | None:
        if not session_id:
            return None
        with self._lock:
            tasks = [Task.from_dict(item) for item in self._read_all().values()]
        matched = [task for task in tasks if task.session_id == session_id]
        if not matched:
            return None
        matched.sort(key=lambda item: item.updated_at, reverse=True)
        return matched[0]

    def find_active_by_chat(self, chat_id: str) -> list[Task]:
        active = {
            TaskStatus.QUEUED,
            TaskStatus.DISPATCHED,
            TaskStatus.RUNNING,
            TaskStatus.PENDING_APPROVAL,
        }
        with self._lock:
            tasks = [Task.from_dict(item) for item in self._read_all().values()]
        return [task for task in tasks if task.chat_id == chat_id and task.status in active]
