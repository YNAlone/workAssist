from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Kimi Code model IDs: https://www.kimi.com/code/docs/en/kimi-code/models
ALLOWED_KIMI_MODELS = ("kimi-for-coding", "k3", "kimi-for-coding-highspeed")
_KIMI_MODEL_ALIASES = {
    "kimi-for-coding": "kimi-for-coding",
    "kimi-for-coding-highspeed": "kimi-for-coding-highspeed",
    "k3": "k3",
    "kimi-k3": "k3",
    "kimi_k3": "k3",
}


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_kimi_model(value: str | None, default: str = "kimi-for-coding") -> str:
    """Normalize user-facing model names (e.g. K3) to Kimi Code API IDs."""
    raw = (value or "").strip()
    if not raw:
        return default
    canonical = _KIMI_MODEL_ALIASES.get(raw.lower())
    if canonical:
        return canonical
    if raw in ALLOWED_KIMI_MODELS:
        return raw
    raise ValueError(
        f"Unsupported model {raw!r}. Choose one of: {', '.join(ALLOWED_KIMI_MODELS)} "
        "(aliases: K3, kimi-k3)."
    )


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    callback_base_url: str
    dry_run: bool
    feishu_verification_token: str
    feishu_app_id: str
    feishu_app_secret: str
    feishu_bot_webhook: str
    feishu_doc_mount_key: str
    feishu_doc_mount_folder: str
    github_token: str
    github_workflow_id: str
    github_api_base: str
    github_dispatch_ref: str
    gitlab_token: str
    gitlab_api_base: str
    gitlab_dispatch_ref: str
    policy_file: Path
    task_store_path: Path
    audit_log_path: Path
    orch_llm_api_key: str
    orch_llm_base_url: str
    orch_llm_model: str
    session_store_path: Path
    session_ttl_minutes: int
    local_worker_enabled: bool
    local_worker_token: str
    local_worker_queue_path: Path
    local_worker_poll_seconds: int
    local_worker_orchestrator_url: str
    anthropic_api_key: str
    anthropic_base_url: str
    anthropic_model: str
    # Enables Feishu's outbound WebSocket event delivery instead of a public webhook.
    feishu_long_connection_enabled: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        root = Path(__file__).resolve().parents[2]
        return cls(
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8080")),
            callback_base_url=os.getenv("CALLBACK_BASE_URL", "http://localhost:8080"),
            dry_run=_bool(os.getenv("AUTOMATION_DRY_RUN"), False),
            feishu_verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", ""),
            feishu_app_id=os.getenv("FEISHU_APP_ID", ""),
            feishu_app_secret=os.getenv("FEISHU_APP_SECRET", ""),
            feishu_bot_webhook=os.getenv("FEISHU_BOT_WEBHOOK", ""),
            feishu_doc_mount_key=os.getenv("FEISHU_DOC_MOUNT_KEY", ""),
            feishu_doc_mount_folder=os.getenv("FEISHU_DOC_MOUNT_FOLDER", "test"),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            github_workflow_id=os.getenv("GITHUB_WORKFLOW_ID", "feishu-claude.yml"),
            github_api_base=os.getenv("GITHUB_API_BASE", "https://api.github.com"),
            github_dispatch_ref=os.getenv("GITHUB_DISPATCH_REF", ""),
            gitlab_token=os.getenv("GITLAB_TOKEN", ""),
            gitlab_api_base=os.getenv("GITLAB_API_BASE", "https://gitlab.thinkingdata.cn/api/v4"),
            gitlab_dispatch_ref=os.getenv("GITLAB_DISPATCH_REF", ""),
            policy_file=Path(os.getenv("POLICY_FILE", root / "config/policy.example.json")),
            task_store_path=Path(os.getenv("TASK_STORE_PATH", root / "data/tasks.json")),
            audit_log_path=Path(os.getenv("AUDIT_LOG_PATH", root / "data/audit.log")),
            orch_llm_api_key=os.getenv("ORCH_LLM_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("KIMI_API_KEY")
            or "",
            orch_llm_base_url=os.getenv("ORCH_LLM_BASE_URL", "https://api.kimi.com/coding/"),
            orch_llm_model=normalize_kimi_model(os.getenv("ORCH_LLM_MODEL"), "kimi-for-coding"),
            session_store_path=Path(os.getenv("SESSION_STORE_PATH", root / "data/sessions.json")),
            session_ttl_minutes=int(os.getenv("SESSION_TTL_MINUTES", "120")),
            local_worker_enabled=_bool(os.getenv("LOCAL_WORKER_ENABLED"), False),
            local_worker_token=os.getenv("LOCAL_WORKER_TOKEN", ""),
            local_worker_queue_path=Path(
                os.getenv("LOCAL_WORKER_QUEUE_PATH", root / "data/local_worker_queue.json")
            ),
            local_worker_poll_seconds=int(os.getenv("LOCAL_WORKER_POLL_SECONDS", "5")),
            local_worker_orchestrator_url=os.getenv("LOCAL_WORKER_ORCHESTRATOR_URL", "").rstrip("/"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY")
            or os.getenv("KIMI_API_KEY")
            or os.getenv("ORCH_LLM_API_KEY")
            or "",
            anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.kimi.com/coding/"),
            anthropic_model=normalize_kimi_model(os.getenv("ANTHROPIC_MODEL"), "kimi-for-coding"),
            feishu_long_connection_enabled=_bool(
                os.getenv("FEISHU_LONG_CONNECTION_ENABLED"), False
            ),
        )
