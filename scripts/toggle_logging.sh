#!/usr/bin/env bash
# ロギングのトグル。状態は tmux 本体の #{pane_pipe} で判定する（自前フラグは使わない）。
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

pane="${1:-$(tmux display-message -p '#{pane_id}')}"
rl_pane_exists "$pane" || exit 0

if rl_pane_piped "$pane"; then
  base="$(rl_get_pane_option "$pane" @rl_base)"
  [ -n "$base" ] || base="$(rl_meta_get "$(rl_meta_file "$pane")" base)" || base=""
  if [ -n "$base" ]; then
    exec "$CURRENT_DIR/stop_logging.sh" "$pane"
  fi
  # 別ツールの pipe を勝手に閉じない（元プラグインはここで壊す）
  rl_display "pane $pane は別のツールに pipe されています - 触りません"
  exit 0
fi

exec "$CURRENT_DIR/start_logging.sh" "$pane"
