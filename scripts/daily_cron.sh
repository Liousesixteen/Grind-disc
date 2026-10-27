#!/bin/bash
set -u
cd "$(dirname "$0")/.." || exit 1
export TZ=Asia/Shanghai
MODE="${1:-daily}"
case "$MODE" in daily|lhb) ;; *) echo "未知模式"; exit 2;; esac
mkdir -p logs
.venv/bin/python scripts/job_runner.py "$MODE" >>logs/daily.log 2>&1
exit $?
