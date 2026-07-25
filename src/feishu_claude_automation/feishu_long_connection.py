from __future__ import annotations

import dataclasses
import threading
from collections.abc import Mapping
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import Any, Protocol

from .config import Settings


class FeishuMessageHandler(Protocol):
    """Minimal interface used by the long-connection listener."""

    def handle_feishu_message(self, payload: dict[str, Any], *, trusted: bool = False) -> dict[str, Any]: ...


class FeishuLongConnection:
    """Receive Feishu events over an outbound WebSocket connection.

    The SDK owns WebSocket authentication, heartbeats, and reconnects. Event handlers
    must return promptly, so this class only normalizes an event and submits the
    existing orchestrator flow to a background executor.
    """

    def __init__(
        self,
        settings: Settings,
        message_handler: FeishuMessageHandler,
        *,
        executor: Executor | None = None,
    ) -> None:
        self.settings = settings
        self.message_handler = message_handler
        self._executor = executor or ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="feishu-event",
        )
        self._thread: threading.Thread | None = None

    def start_background(self) -> None:
        """Start the blocking SDK client in a daemon thread.

        Keeping it separate from the HTTP server lets the latter remain available for
        localhost-only Local Worker callbacks.
        """
        if self._thread and self._thread.is_alive():
            return
        if not self.settings.feishu_app_id or not self.settings.feishu_app_secret:
            raise RuntimeError(
                "FEISHU_APP_ID and FEISHU_APP_SECRET are required for long connection mode"
            )
        # Fail during startup instead of hiding a missing optional dependency inside
        # a daemon thread where the service would otherwise look healthy.
        sdk = self._load_sdk()
        self._thread = threading.Thread(
            target=self._run_client,
            args=(sdk,),
            name="feishu-long-connection",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _load_sdk() -> Any:
        """Create the optional SDK client only when long connection mode is enabled."""
        try:
            import lark_oapi as lark
        except ImportError as exc:  # pragma: no cover - exercised only in a deployed runtime
            raise RuntimeError(
                "lark-oapi is required for FEISHU_LONG_CONNECTION_ENABLED=true. "
                "Install project dependencies before starting the service."
            ) from exc
        return lark

    def _run_client(self, lark: Any) -> None:
        """Run the blocking SDK client after startup dependency validation."""

        event_handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        client = lark.ws.Client(
            self.settings.feishu_app_id,
            self.settings.feishu_app_secret,
            event_handler=event_handler,
        )
        client.start()

    def _on_message(self, event: Any) -> None:
        """Acknowledge quickly and process the potentially slow task asynchronously."""
        payload = self.to_event_payload(event)
        self._executor.submit(self._dispatch_message, payload)
        return None

    def _dispatch_message(self, payload: dict[str, Any]) -> None:
        # The SDK authenticated the persistent connection before delivering this event.
        # Keep the HTTP verification-token check enabled for public webhook requests.
        self.message_handler.handle_feishu_message(payload, trusted=True)

    @classmethod
    def to_event_payload(cls, event: Any) -> dict[str, Any]:
        """Convert SDK event models into the legacy webhook payload used by the app.

        lark-oapi model versions may expose either ``to_dict`` or object attributes;
        normalizing both formats keeps the core orchestrator independent of the SDK.
        """
        payload = cls._to_plain(event)
        if not isinstance(payload, dict):
            raise ValueError("Feishu long connection event must serialize to an object")

        # The SDK normally returns the same schema-2 envelope as a webhook.
        if isinstance(payload.get("event"), dict):
            payload.setdefault("header", {})
            payload["header"].setdefault("event_type", "im.message.receive_v1")
            return payload

        # Be defensive with SDK versions that expose only the event body at top level.
        header = payload.pop("header", {})
        if not isinstance(header, dict):
            header = {}
        header.setdefault("event_type", "im.message.receive_v1")
        return {"header": header, "event": payload}

    @classmethod
    def _to_plain(cls, value: Any) -> Any:
        """Recursively turn SDK model objects into JSON-compatible values."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Mapping):
            return {str(key): cls._to_plain(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._to_plain(item) for item in value]
        if dataclasses.is_dataclass(value):
            return cls._to_plain(dataclasses.asdict(value))
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return cls._to_plain(to_dict())
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, dict):
            return {
                str(key): cls._to_plain(item)
                for key, item in attributes.items()
                if not key.startswith("_")
            }
        return str(value)
