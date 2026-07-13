from __future__ import annotations

"""Feishu multi-bot channel (M3).

M1 keeps the legacy single-bot routes in feishu_claude_automation.server.
This module documents the target path layout:

  /channels/feishu/{agent}/events
  /channels/feishu/{agent}/actions
  /channels/feishu/doc/oauth/callback
"""

from typing import Any


class FeishuMultiBotChannel:
    name = "feishu"

    def __init__(self, bots: dict[str, Any]) -> None:
        self.bots = bots

    def send_text(self, chat_id: str, text: str, *, reply_to: str = "", agent_id: str = "orchestra") -> dict[str, Any]:
        bot = self.bots.get(agent_id)
        if bot is None:
            return {"skipped": True, "reason": f"bot not configured: {agent_id}"}
        return bot.send_text(chat_id, text, reply_to=reply_to)

    def send_card(self, chat_id: str, card: dict[str, Any], *, agent_id: str = "orchestra") -> dict[str, Any]:
        bot = self.bots.get(agent_id)
        if bot is None:
            return {"skipped": True, "reason": f"bot not configured: {agent_id}"}
        return bot.send_card(chat_id, card)
