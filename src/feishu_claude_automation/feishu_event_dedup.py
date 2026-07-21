from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


class FeishuEventDedup:
    """Drop duplicate Feishu webhook deliveries (retries use the same event_id)."""

    def __init__(self, path: Path, *, ttl_seconds: int = 3600) -> None:
        self.path = path
        self.ttl_seconds = max(60, ttl_seconds)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    @contextmanager
    def _locked_store(self, *, mutate: bool) -> Iterator[dict[str, str]]:
        handle = self.path.open("r+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            raw = handle.read()
            store: dict[str, Any] = json.loads(raw) if raw.strip() else {}
            if not isinstance(store, dict):
                store = {}
            yield store  # type: ignore[misc]
            if mutate:
                handle.seek(0)
                handle.truncate()
                handle.write(json.dumps(store, ensure_ascii=False, indent=2))
                handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _prune(self, store: dict[str, Any]) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.ttl_seconds)
        stale = []
        for key, ts in store.items():
            try:
                seen = datetime.fromisoformat(str(ts))
            except ValueError:
                stale.append(key)
                continue
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            if seen < cutoff:
                stale.append(key)
        for key in stale:
            store.pop(key, None)

    def try_claim(self, key: str) -> bool:
        """Return True if this key is new; False if already processed recently."""
        key = key.strip()
        if not key:
            return True
        now = datetime.now(timezone.utc).isoformat()
        with self._locked_store(mutate=True) as store:
            self._prune(store)
            if key in store:
                return False
            store[key] = now
            return True
