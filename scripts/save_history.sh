#!/usr/bin/env bash
# ペインのスクロールバック全体（history-limit の範囲）をファイルに保存する。
# 注意: 確実な記録は pipe-pane ロギングの方。これは「今からでも過去分を救う」用途。
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

pane="${1:-$(tmux display-message -p '#{pane_id}')}"
rl_pane_exists "$pane" || exit 0

base="$(rl_base_name "$pane")"
file="$base.history.txt"
umask 077

args=(-p -J -S -)
if [ "$(rl_get_option @rl_history_escapes off)" = "on" ]; then
  args=(-e -p -J -S -)
fi

if tmux capture-pane -t "$pane" "${args[@]}" > "$file"; then
  rl_display "history saved: $file"
else
  rm -f "$file"
  rl_display "history の取得に失敗しました ($pane)"
  exit 1
fi
