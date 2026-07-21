#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
export PYTHONPATH=src
exec python3 -m feishu_claude_automation.server "$@"
