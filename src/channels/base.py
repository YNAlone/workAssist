from __future__ import annotations

from typing import Any, Protocol


class Channel(Protocol):
    name: str

    def send_text(self, chat_id: str, text: str, *, reply_to: str = "") -> dict[str, Any]: ...

    def send_card(self, chat_id: str, card: dict[str, Any]) -> dict[str, Any]: ...
