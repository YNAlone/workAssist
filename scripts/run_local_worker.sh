#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
exec python3 -m feishu_claude_automation.local_worker "$@"
