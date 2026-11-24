#!/usr/bin/env bash
# 一键部署：./deploy.sh [额外参数]，默认读取同目录 config.json（不存在则用 config.example.json）
set -euo pipefail
cd "$(dirname "$0")"
CFG=config.json; [ -f "$CFG" ] || CFG=config.example.json
exec python3 deploy.py "${1:-deploy}" -c "$CFG" "${@:2}"
