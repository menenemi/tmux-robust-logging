#!/usr/bin/env bash
# ペインのスクロールバックを消去する。
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

pane="${1:-$(tmux display-message -p '#{pane_id}')}"
rl_pane_exists "$pane" || exit 0

tmux clear-history -t "$pane"
rl_display "history cleared ($pane)"
