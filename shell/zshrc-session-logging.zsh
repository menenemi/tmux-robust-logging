# zshrc-session-logging.zsh — tmux を使わずに script(1) で全セッションを記録する。
# 使い方: ~/.zshrc の「できるだけ先頭」に  source /path/to/zshrc-session-logging.zsh
#
# 記録されるもの: プロンプト・入力したコマンド（エコーバック）・全出力・色 = 生バイト。
# 整形（ANSI 除去等）は次回ログイン時に tmux-robust-logging の sanitize.sh で行う。
#
# ガード条件（重要）:
#  - 対話シェルかつ TTY があるときだけ。scp / sftp / rsync / git-over-ssh のような
#    非対話 ssh セッションを script で包むとプロトコルが壊れるため
#  - 二重記録防止の環境変数ガード（exec 後の内側シェルは再ラップしない）
#  - tmux の中では起動しない（tmux 側のロギングと二重になるだけなので）

if [[ -o interactive && -t 0 && -z "$TERM_SESSION_LOGGED" && -z "$TMUX" ]] \
   && command -v script >/dev/null 2>&1; then
  export TERM_SESSION_LOGGED=1

  _rl_logdir="$HOME/term-logs"
  mkdir -p -- "$_rl_logdir" && chmod 700 -- "$_rl_logdir"

  # ---- 前回セッションの raw を整形（次回ログイン時 sweep 方式）--------------
  # ファイル名末尾の PID がまだ生きている = 記録中のセッションなので触らない。
  _rl_san="$HOME/tmux-robust-logging/scripts/sanitize.sh"
  if [[ -x "$_rl_san" ]]; then
    for _rl_f in "$_rl_logdir"/*.raw.log(N); do
      _rl_pid="${${_rl_f:t}%.raw.log}"
      _rl_pid="${_rl_pid##*_}"
      if [[ "$_rl_pid" == <-> ]] && kill -0 "$_rl_pid" 2>/dev/null; then
        continue # まだ記録中のセッション
      fi
      if [[ ! -e "${_rl_f%.raw.log}.log" ]]; then
        if "$_rl_san" < "$_rl_f" > "${_rl_f%.raw.log}.log" \
           && [[ -s "${_rl_f%.raw.log}.log" ]]; then
          rm -f -- "$_rl_f" # 整形に成功したときだけ raw を消す
        else
          rm -f -- "${_rl_f%.raw.log}.log" # 失敗時は raw を残す
        fi
      fi
    done
  fi

  # ---- 記録開始 --------------------------------------------------------------
  # exec で script に置き換わるため PID($$) は script に引き継がれる =
  # ファイル名の PID で「記録中かどうか」を後から判定できる。
  _rl_log="$_rl_logdir/$(date +%Y%m%dT%H%M%S)_$$.raw.log"
  umask 077
  # util-linux (Linux): -q ヘッダ抑制, -f 1バイトごとに flush（クラッシュ耐性）
  # macOS/BSD の script は -f がないので:  exec script -q "$_rl_log"
  exec script -qf "$_rl_log"
fi
