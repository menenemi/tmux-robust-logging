#!/usr/bin/env bash
# ウォッチドッグを（未起動なら）tmux サーバから完全に切り離して起動する。
# プラグインロード時に @rl_exit_sweep が on（既定）のとき呼ばれる。
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

pid="$(tmux display-message -p '#{pid}' 2>/dev/null)" || exit 0
case "$pid" in ''|*[!0-9]*) exit 0 ;; esac
sd="$(rl_state_dir)"

# 既に生きたウォッチドッグがいれば何もしない
old="$(cat "$sd/watchdog.pid" 2>/dev/null)" || old=""
case "$old" in *[!0-9]*) old="" ;; esac
if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
  exit 0
fi

# setsid があれば新セッションで、なければ nohup で、サーバ終了の巻き添えを回避
if command -v setsid >/dev/null 2>&1; then
  setsid "$CURRENT_DIR/watchdog.sh" "$pid" "$sd" </dev/null >/dev/null 2>&1 &
else
  nohup "$CURRENT_DIR/watchdog.sh" "$pid" "$sd" </dev/null >/dev/null 2>&1 &
fi
disown 2>/dev/null || true
exit 0
