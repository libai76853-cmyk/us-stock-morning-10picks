#!/usr/bin/env bash
# 用法：./run.sh [serve|login official|login unofficial|probe|token]
# 第一次运行会在本目录建 .venv 并安装依赖。
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
# 可选：把设置写在本目录的 .env 里（不提交），例如 TPSL_BACKEND=official
if [ -f .env ]; then set -a; . ./.env; set +a; fi
exec .venv/bin/python -m tpsl "$@"
