# emaki シェル統合（bash） — tmux-robust-logging
#
# ~/.bashrc に 1 行追加するだけ:
#   source ~/.tmux/plugins/tmux-robust-logging/shell/emaki-integration.bash
#
# 有効になるもの:
#   1. `emaki` コマンド（このプラグインの bin/emaki を python3 で呼ぶ）
#   2. コマンド境界の印（OSC 133 A/B/C/D、OSC 7）。画面には何も出ないが pipe-pane のログには残り、
#      emaki がコマンドの区切り・終了コード・ディレクトリを正確に取れる。
#      （bash は実行するコマンド文字列を確実に取る手段が無いので、文字列は入力行から読む。
#        プロンプトの終わりに B の印を付けて、プロンプトと入力の境目を正確にする）
#
# 印には、シェルごとに作る合言葉（;emaki=<nonce>）を付け、同じ合言葉を手元のファイル
# （${XDG_STATE_HOME:-~/.local/state}/emaki/nonces）にだけ記録する。コマンドの出力に偽の印が
# 混ざっていても、合言葉が記録と合わないので emaki は無視する。

[[ $- == *i* ]] || return 0

# ---- emaki コマンド ------------------------------------------------------------------------
_EMAKI_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd -P)
emaki() { command python3 "$_EMAKI_ROOT/bin/emaki" "$@"; }

[[ -n ${_EMAKI_SI-} ]] && return 0
_EMAKI_SI=1
_emaki_first=1
_emaki_seq=0   # 実行したコマンドの通し番号（印に付ける。PS0 を表示するたびに 1 増える）

# ---- このシェルの合言葉を作って記録する ------------------------------------------------------
if [[ -n ${SRANDOM-} ]]; then   # bash 5.1+（getrandom 由来）
  printf -v _emaki_nonce '%08x%08x' "$SRANDOM" "$SRANDOM"
else
  _emaki_t=${EPOCHREALTIME//[!0-9]/}   # 小数点がロケールで変わるので数字だけにする
  _emaki_t=${_emaki_t: -6}
  printf -v _emaki_nonce '%04x%04x%04x%04x' "$RANDOM" "$(( (RANDOM ^ $$) & 0xffff ))" \
    "$(( (RANDOM ^ 10#${_emaki_t:-0}) & 0xffff ))" "$RANDOM"
fi
_emaki_f=${EMAKI_NONCE_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/emaki/nonces}
[[ -d ${_emaki_f%/*} ]] || mkdir -p -m 700 -- "${_emaki_f%/*}" 2>/dev/null
_emaki_srv=-
if [[ -n ${TMUX-} ]]; then _emaki_srv=${TMUX#*,}; _emaki_srv=${_emaki_srv%%,*}; fi
# 親シェル（このシェルを起動した、統合ありのシェル。入れ子・exec の判定に使う）
_emaki_parent=${EMAKI_SHELL_NONCE:--}
[[ $_emaki_parent =~ ^[0-9a-f]{8,32}$ ]] || _emaki_parent=-
# 1 合言葉 tmuxサーバPID ペインID 記録時刻 シェルPID 親の合言葉
{ printf '1 %s %s %s %(%s)T %s %s\n' "$_emaki_nonce" "${_emaki_srv:--}" "${TMUX_PANE:--}" -1 "$$" \
    "$_emaki_parent" >> "$_emaki_f"; } 2>/dev/null
export EMAKI_SHELL_NONCE=$_emaki_nonce
unset _emaki_f _emaki_srv _emaki_t _emaki_parent

# OSC の中身に入れても安全な形にする（\ と ; と制御文字を \xNN に）
_emaki_esc() {
  local LC_ALL=C s=$1 out= ch hex i
  for (( i = 0; i < ${#s}; i++ )); do
    ch=${s:i:1}
    case $ch in
      \\) out+='\\' ;;
      ';') out+='\x3b' ;;
      [[:cntrl:]]) printf -v hex '%02x' "'$ch"; out+="\\x$hex" ;;
      *) out+=$ch ;;
    esac
  done
  REPLY=$out
}

# 印に付ける「合言葉:コマンド番号」。番号は戻らないので、古いログを cat したときに流れる
# 同じシェルの過去の印も見分けられる
# プロンプトの直前: 直前のコマンドの終了（D）→ カレントディレクトリ → プロンプト開始（A）
# （空 Enter のときも D を出すが、C が無い D は emaki 側で無視される）
_emaki_precmd() {
  local ec=$?
  [[ -z $_emaki_first ]] && printf '\e]133;D;%s;emaki=%s:%s\a' "$ec" "$_emaki_nonce" "$_emaki_seq"
  _emaki_first=
  _emaki_esc "$PWD"
  printf '\e]7;file://%s%s\a\e]133;A;emaki=%s:%s\a' "${HOSTNAME-}" "$REPLY" "$_emaki_nonce" "$_emaki_seq"
  return $ec
}

# プロンプトの終わり（入力の始まり）に B の印。PS1 を毎回作り直す設定でも付くよう、最後に実行する
_emaki_b='\[\e]133;B;emaki='"$_emaki_nonce"'\a\]'
_emaki_ps1() {
  local ec=$?
  PS1=${PS1//"$_emaki_b"/}$_emaki_b
  return $ec
}

# Kali の既定の ~/.bashrc は PROMPT_COMMAND="PROMPT_COMMAND=echo"（2 回目以降のプロンプトの前に空行を
# 出す仕掛け）。そのまま包むと 1 回目で PROMPT_COMMAND 全体が echo に置き換わり印が出なくなるので、
# 同じ動きの関数に差し替える（この行より後で PROMPT_COMMAND を代入し直す設定があると印は出なくなる）
_emaki_nl() { [[ -n ${_emaki_nl_on-} ]] && echo; _emaki_nl_on=1; }
[[ ${PROMPT_COMMAND-} == "PROMPT_COMMAND=echo" ]] && PROMPT_COMMAND=_emaki_nl

if [[ "$(declare -p PROMPT_COMMAND 2>/dev/null)" == "declare -a"* ]]; then
  PROMPT_COMMAND=(_emaki_precmd "${PROMPT_COMMAND[@]}" _emaki_ps1)   # bash 5.1+ の配列形式
else
  PROMPT_COMMAND="_emaki_precmd"$'\n'"${PROMPT_COMMAND-}"$'\n'"_emaki_ps1"
fi
# コマンド実行の直前（出力の開始）: PS0 は Enter 後・実行前に表示される（表示のたびに番号を進める）
PS0="${PS0-}"'\e]133;C;emaki='"$_emaki_nonce"':$((++_emaki_seq))\a'
