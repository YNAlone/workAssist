from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    github_token: str
    github_workflow_id: str
    github_api_base: str
    policy_file: Path
    task_store_path: Path
    audit_log_path: Path

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
            github_token=os.getenv("GITHUB_TOKEN", ""),
            github_workflow_id=os.getenv("GITHUB_WORKFLOW_ID", "feishu-claude.yml"),
            github_api_base=os.getenv("GITHUB_API_BASE", "https://api.github.com"),
            policy_file=Path(os.getenv("POLICY_FILE", root / "config/policy.example.json")),
            task_store_path=Path(os.getenv("TASK_STORE_PATH", root / "data/tasks.json")),
            audit_log_path=Path(os.getenv("AUDIT_LOG_PATH", root / "data/audit.log")),
        )
