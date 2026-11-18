#!/bin/zsh
# 啟動 Skill Explorer（網頁 + 內建每日排程）
cd "$(dirname "$0")"
[ -f .env ] && set -a && source .env && set +a
exec python3 -m uvicorn backend.app:app --host 127.0.0.1 --port "${PORT:-5000}"
