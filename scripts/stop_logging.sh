#!/usr/bin/env bash
# ペインのロギングを停止し、finalize（結合・整形・掃除）する。
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

pane="${1:-$(tmux display-message -p '#{pane_id}')}"

base="$(rl_get_pane_option "$pane" @rl_base)"
if [ -z "$base" ]; then
  base="$(rl_meta_get "$(rl_meta_file "$pane")" base)" || base=""
fi
if [ -z "$base" ]; then
  rl_display "pane $pane はこのプラグインでロギングされていません"
  exit 0
fi

if rl_pane_exists "$pane" && rl_pane_piped "$pane"; then
  tmux pipe-pane -t "$pane" # 引数なし = pipe を閉じる
fi

exec "$CURRENT_DIR/finalize.sh" "$pane"
