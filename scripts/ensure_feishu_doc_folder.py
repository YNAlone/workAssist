#!/usr/bin/env python3
"""Ensure the configured Feishu doc mount folder exists and print its token."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from feishu_claude_automation.config import Settings
from feishu_claude_automation.feishu import FeishuClient
from feishu_claude_automation.feishu_docs import FeishuDocService


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default="", help="Folder name under cloud root (default: env or test)")
    args = parser.parse_args()

    settings = Settings.from_env()
    folder_name = args.folder or settings.feishu_doc_mount_folder or "test"
    service = FeishuDocService(FeishuClient(settings), mount_folder=folder_name)
    folder = service.ensure_folder(folder_name, parent_folder_token="")
    print(f"folder_name={folder_name}")
    print(f"folder_token={folder.token}")
    print(f"folder_url={folder.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
