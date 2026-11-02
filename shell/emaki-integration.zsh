# emaki シェル統合（zsh） — tmux-robust-logging
#
# ~/.zshrc に 1 行追加するだけ:
#   source ~/.tmux/plugins/tmux-robust-logging/shell/emaki-integration.zsh
#
# 有効になるもの:
#   1. `emaki` コマンド（このプラグインの bin/emaki を python3 で呼ぶ）
#   2. コマンド境界の印（OSC 133 A/C/D、OSC 633;E、OSC 7）
#      画面には何も出ない（tmux が解釈して捨てる）が、pipe-pane のログには生のまま残るので、
#      emaki がコマンドの区切り・終了コード・実行したディレクトリを推定せず正確に取れる。
#      tmux 3.4 以降ならコピーモードのプロンプト間ジャンプ（next-prompt / previous-prompt）にも効く。
#
# 印には、シェルごとに作る合言葉（;emaki=<nonce>）を付け、同じ合言葉を手元のファイル
# （${XDG_STATE_HOME:-~/.local/state}/emaki/nonces）にだけ記録する。コマンドの出力
# （攻撃対象のサーバが返す文字列や、ログファイルの cat など）に偽の印が混ざっていても、
# 合言葉が記録と合わないので emaki は無視する。
#
# p10k の instant prompt と併用可（読み込み時には何も出力しない）。

[[ -o interactive ]] || return 0

# ---- emaki コマンド ------------------------------------------------------------------------
typeset -g _EMAKI_ROOT=${${(%):-%x}:A:h:h}
emaki() { command python3 "$_EMAKI_ROOT/bin/emaki" "$@" }

(( ${+_EMAKI_SI} )) && return 0
typeset -g _EMAKI_SI=1
typeset -gi _emaki_running=0 _emaki_seq=0   # _emaki_seq: 実行したコマンドの通し番号（印に付ける）

# ---- このシェルの合言葉を作って記録する ------------------------------------------------------
() {
  emulate -L zsh
  zmodload zsh/datetime 2>/dev/null
  local -i a b
  if zmodload zsh/random 2>/dev/null && (( ${+SRANDOM} )); then
    a=SRANDOM b=SRANDOM
  else
    local us=${${EPOCHREALTIME:-0.0}#*.}
    a=$(( (RANDOM << 17) ^ (RANDOM << 2) ^ $$ ))
    b=$(( (RANDOM << 17) ^ (RANDOM << 2) ^ 10#${us:-0} ))
  fi
  local h1=${(l:8::0:)$(( [##16] a & 0xffffffff ))} h2=${(l:8::0:)$(( [##16] b & 0xffffffff ))}
  typeset -g _emaki_nonce=${(L)h1}${(L)h2}
  local f=${EMAKI_NONCE_FILE:-${XDG_STATE_HOME:-$HOME/.local/state}/emaki/nonces}
  [[ -d ${f:h} ]] || mkdir -p -m 700 -- ${f:h} 2>/dev/null
  local srv=-
  [[ -n $TMUX ]] && srv=${${TMUX#*,}%%,*}
  # 親シェル（このシェルを起動した、統合ありのシェル。入れ子・exec の判定に使う）
  local parent=${EMAKI_SHELL_NONCE:--}
  [[ $parent =~ '^[0-9a-f]{8,32}$' ]] || parent=-
  # 1 合言葉 tmuxサーバPID ペインID 記録時刻 シェルPID 親の合言葉
  { print -r -- "1 $_emaki_nonce ${srv:--} ${TMUX_PANE:--} ${EPOCHSECONDS:-0} $$ $parent" >> $f } 2>/dev/null
  export EMAKI_SHELL_NONCE=$_emaki_nonce
}

# OSC の中身に入れても安全な形にする（\ と ; と制御文字を \xNN に。OSC を途中で終わらせない）
# （長いコマンドでも遅くならないよう、1 文字ずつではなく置換でまとめて行う）
_emaki_esc() {
  emulate -L zsh
  local LC_ALL=C s=$1 c
  s=${s//\\/\\\\}
  s=${s//;/\\x3b}
  if [[ $s == *[[:cntrl:]]* ]]; then
    for c in ${(u)${(s::)${s//[^[:cntrl:]]/}}}; do
      s=${s//$c/\\x${(l:2::0:)${(L)$(( [##16] #c ))}}}
    done
  fi
  REPLY=$s
}

# 印に付ける「合言葉:コマンド番号」。番号は戻らないので、古いログを cat したときに流れる
# 同じシェルの過去の印も見分けられる
# プロンプトを出す直前: 直前のコマンドの終了（D + 終了コード）→ カレントディレクトリ → プロンプト開始（A）
_emaki_precmd() {
  local ec=$? tag="emaki=${_emaki_nonce}:${_emaki_seq}"
  if (( _emaki_running )); then
    builtin print -nr -- $'\e]133;D;'"${ec};${tag}"$'\a'
    _emaki_running=0
  fi
  _emaki_esc "$PWD"
  builtin print -nr -- $'\e]7;file://'"${HOST}${REPLY}"$'\a\e]133;A;'"${tag}"$'\a'
  return $ec
}

# Enter を押してコマンドが始まる直前: 実行するコマンド文字列（E）→ 出力の開始（C）
_emaki_preexec() {
  (( ++_emaki_seq ))
  local tag="emaki=${_emaki_nonce}:${_emaki_seq}"
  _emaki_esc "$1"
  builtin print -nr -- $'\e]633;E;'"${REPLY};${tag}"$'\a\e]133;C;'"${tag}"$'\a'
  _emaki_running=1
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _emaki_preexec
# precmd は先頭に置く（$? はどのフックでも同じだが、他のフックの出力より先に印を出すため）
precmd_functions=(_emaki_precmd ${precmd_functions:#_emaki_precmd})
