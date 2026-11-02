#!/usr/bin/env bash
# フックから呼ばれる: @rl_auto_start が on なら新しいペインのロギングを開始する。
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

[ "$(rl_get_option @rl_auto_start off)" = "on" ] || exit 0

pane="${1:-}"
if [ -n "$pane" ] && rl_pane_exists "$pane"; then
  exec "$CURRENT_DIR/start_logging.sh" "$pane"
fi

# hook_pane が取れないケース（detached での session-created など）:
# 推測で 1 ペインを選ばず、pipe されていない全ペインをカバーする
tmux list-panes -a -F '#{pane_id}' | while IFS= read -r p; do
  "$CURRENT_DIR/start_logging.sh" "$p" >/dev/null 2>&1 || true
done
