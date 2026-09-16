#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v uv >/dev/null 2>&1; then
  echo "請先安裝 uv：https://docs.astral.sh/uv/getting-started/installation/"
  exit 1
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "請先安裝 Node.js 22 或更新版本（需包含 npm）。"
  exit 1
fi
uv sync --frozen
.venv/bin/python -m playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build
echo "ComputerUSE 工作台：http://127.0.0.1:${COMPUTERUSE_PORT:-8765}"
exec .venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port "${COMPUTERUSE_PORT:-8765}"
