#!/usr/bin/env bash
# シミュレーションのセッションで emaki とロガーを確かめる（tmux・実ログは要らない）。
# 使い方: bash tests/run_all.sh
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
export PYTHONIOENCODING=utf-8 PYTHONDONTWRITEBYTECODE=1
rc=0
for t in cases.py cases_bg.py cases_lim.py test_chunks.py test_raw_export.py; do
  printf '%-22s ' "$t"
  python "$HERE/$t" "$ROOT" | tail -1
  [ "${PIPESTATUS[0]}" -eq 0 ] || rc=1
done
printf '%-22s ' "test_logger.sh"
if bash "$HERE/test_logger.sh" "$ROOT" > "$HERE/logger.out" 2>&1; then
  echo "ok"
else
  cat "$HERE/logger.out"
  rc=1
fi
rm -f "$HERE/logger.out"
exit $rc
