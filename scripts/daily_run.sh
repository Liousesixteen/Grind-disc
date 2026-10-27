#!/bin/bash
# Explicit date is propagated to every task. Preserve failures while independent tasks continue.
set -u
cd "$(dirname "$0")/.." || exit 1
export TZ=Asia/Shanghai
export PYTHONPATH="$PWD/scripts${PYTHONPATH:+:$PYTHONPATH}"
TASK_ARGS=("$@")
PREVIOUS_ARG=""
for ARG in "$@"; do
  if [ "$PREVIOUS_ARG" = "--db" ]; then export TRADING_DB="$ARG"; fi
  PREVIOUS_ARG="$ARG"
done
if [ "$#" -eq 0 ]; then
  TARGET=$(.venv/bin/python - <<'PY'
from data_runtime import connect, ProClient, target_date
c=connect()
try: print(target_date(c,ProClient()))
finally: c.close()
PY
) || exit 1
  TASK_ARGS=(--date "$TARGET")
fi
RC=0
.venv/bin/python scripts/ts_daily.py --sources daily,moneyflow,kpl,ths "${TASK_ARGS[@]}" || RC=1
.venv/bin/python scripts/ts_etf.py --daily --limit --share --margin --views --days 1 "${TASK_ARGS[@]}" || RC=1
if [ "$RC" -eq 0 ]; then
  .venv/bin/python scripts/theme_cross.py || RC=1
  .venv/bin/python scripts/theme_trend.py || RC=1
fi
exit "$RC"
