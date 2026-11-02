#!/usr/bin/env bash
# tmux-robust-logging: 取りこぼしのないペインロギング（raw ファースト設計）。
# tmux-plugins/tmux-logging を下敷きに、状態管理・文字化け・切断まわりを堅牢化。

CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS="$CURRENT_DIR/scripts"
. "$SCRIPTS/helpers.sh"

if ! rl_version_ok; then
  tmux display-message "tmux-robust-logging: tmux 3.1 以上が必要です (pane options / indexed hooks)"
  exit 0
fi

rl_bind() { # <key> <script>
  local q
  q="$(rl_fmt_escape "$(rl_shq "$SCRIPTS/$2")")"
  # run-shell はコマンド文字列を format 展開するので、押下時のアクティブペインが入る
  tmux bind-key "$1" run-shell -b "$q '#{pane_id}'"
}

rl_bind "$(rl_get_option @rl_key_toggle  P)"   toggle_logging.sh
rl_bind "$(rl_get_option @rl_key_screen  M-p)" screen_capture.sh
rl_bind "$(rl_get_option @rl_key_history M-P)" save_history.sh
rl_bind "$(rl_get_option @rl_key_clear   M-c)" clear_history.sh

# emaki: このセッションのそれまでのログを 1 枚の HTML に（python3 が必要。無ければ割り当てない）
emaki_key="$(rl_get_option @rl_key_emaki M-e)"
if [ "$emaki_key" != "none" ] && command -v python3 >/dev/null 2>&1; then
  # パスの '#' だけを format 用にエスケープし、#{pane_id} は押下時に展開させる
  emaki_cmd="python3 $(rl_fmt_escape "$(rl_shq "$CURRENT_DIR/bin/emaki")") --from-tmux '#{pane_id}'"
  [ "$(rl_get_option @rl_emaki_open off)" = "on" ] && emaki_cmd="$emaki_cmd --open"
  [ "$(rl_get_option @rl_emaki_embed_font off)" = "on" ] && emaki_cmd="$emaki_cmd --embed-font"
  # 結果も失敗もステータス行（display-message）で知らせる。run-shell -b は出力があるか、
  # 0 以外で終わる（"'…' returned 1" を出す）とアクティブなペインを view mode に切り替えて
  # しまうので、万一の出力は捨て、終了コードも 0 にする
  tmux bind-key "$emaki_key" run-shell -b "$emaki_cmd >/dev/null 2>&1 || true"
fi

# 直接 argv として渡す用（tmux はこの文字列を format 展開して sh へ渡すだけ）
fin_direct="$(rl_fmt_escape "$(rl_shq "$SCRIPTS/finalize.sh")")"
# set-hook に保存する用: 発火時に tmux の二重引用符レイヤをもう一度通るため、
# sh クォートに加えて \ " $ もエスケープしておく（パスに ' や $ があっても壊れない）
fin_hook="$(rl_fmt_escape "$(rl_dq_escape "$(rl_shq "$SCRIPTS/finalize.sh")")")"
auto_hook="$(rl_fmt_escape "$(rl_dq_escape "$(rl_shq "$SCRIPTS/autostart.sh")")")"

# インデックス付きフックを使い、ユーザー自身のフックを上書きしない。
# pane-exited/pane-died はプロセス自身の終了時のみ発火する。kill-pane /
# kill-window / kill-session ではどちらも発火しない（tmux は通知を出さない）ため、
# レイアウト変化・ウィンドウ削除・セッション終了で --sweep を回して取りこぼしを回収する。
tmux set-hook -g 'pane-exited[47110]'           "run-shell -b \"$fin_hook '#{hook_pane}'\""
tmux set-hook -g 'pane-died[47111]'             "run-shell -b \"$fin_hook '#{hook_pane}'\""
tmux set-hook -g 'after-split-window[47112]'    "run-shell -b \"$auto_hook '#{hook_pane}'\""
tmux set-hook -g 'after-new-window[47113]'      "run-shell -b \"$auto_hook '#{hook_pane}'\""
tmux set-hook -g 'session-created[47114]'       "run-shell -b \"$auto_hook '#{hook_pane}'\""
tmux set-hook -g 'window-layout-changed[47115]' "run-shell -b \"$fin_hook --sweep\""
tmux set-hook -g 'window-unlinked[47116]'       "run-shell -b \"$fin_hook --sweep\""
tmux set-hook -g 'session-closed[47117]'        "run-shell -b \"$fin_hook --sweep\""

# 前回サーバ（kill-server / クラッシュ）の未 finalize ログも含めて回収
tmux run-shell -b "$fin_direct --sweep"

# サーバ終了（最後の exit / kill-server）を外から検知して自動 finalize する
# ウォッチドッグを常駐させる（既定 on。off にしても次回ロード時の sweep で回収される）。
# @rl_live_clean（ロギング中の逐次整形）もこのウォッチドッグが担う。
if [ "$(rl_get_option @rl_exit_sweep on)" = "on" ] \
   || [ "$(rl_get_option @rl_live_clean 0)" != "0" ]; then
  "$SCRIPTS/spawn_watchdog.sh" >/dev/null 2>&1 || true
fi

# auto_start が on なら既存ペインもすべてロギング開始
if [ "$(rl_get_option @rl_auto_start off)" = "on" ]; then
  tmux list-panes -a -F '#{pane_id}' | while IFS= read -r p; do
    "$SCRIPTS/autostart.sh" "$p" >/dev/null 2>&1 || true
  done
fi
