from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ConversationSession, SessionStatus, utc_now


class SessionStore:
    def __init__(self, path: Path, ttl_minutes: int = 120) -> None:
        self.path = path
        self.ttl_minutes = ttl_minutes
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def _read_all(self) -> dict[str, dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write_all(self, data: dict[str, dict]) -> None:
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def _is_expired(self, session: ConversationSession) -> bool:
        if session.status == SessionStatus.CLOSED:
            return True
        try:
            updated = datetime.fromisoformat(session.updated_at)
        except ValueError:
            return False
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - updated > timedelta(minutes=self.ttl_minutes)

    def save(self, session: ConversationSession) -> ConversationSession:
        with self._lock:
            data = self._read_all()
            session.updated_at = utc_now()
            data[session.id] = session.to_dict()
            self._write_all(data)
        return session

    def get(self, session_id: str) -> ConversationSession | None:
        with self._lock:
            raw = self._read_all().get(session_id)
        if not raw:
            return None
        session = ConversationSession.from_dict(raw)
        if self._is_expired(session):
            session.status = SessionStatus.CLOSED
            return self.save(session)
        return session

    def get_active(self, chat_id: str, requester_id: str) -> ConversationSession | None:
        with self._lock:
            sessions = [ConversationSession.from_dict(item) for item in self._read_all().values()]
        candidates = [
            session
            for session in sessions
            if session.chat_id == chat_id
            and session.requester_id == requester_id
            and session.status != SessionStatus.CLOSED
            and not self._is_expired(session)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.updated_at, reverse=True)
        return candidates[0]

    def close(self, session: ConversationSession) -> ConversationSession:
        session.status = SessionStatus.CLOSED
        return self.save(session)
