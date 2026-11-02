#!/usr/bin/env bash
# 現在表示中の画面のみをテキストとして保存する（スクリーンショット相当）。
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

pane="${1:-$(tmux display-message -p '#{pane_id}')}"
rl_pane_exists "$pane" || exit 0

base="$(rl_base_name "$pane")"
file="$base.screen.txt"
umask 077

if tmux capture-pane -t "$pane" -p > "$file"; then
  rl_display "screen saved: $file"
else
  rm -f "$file"
  rl_display "screen capture に失敗しました ($pane)"
  exit 1
fi
