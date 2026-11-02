#!/usr/bin/env bash
# tmux-robust-logging: shared helpers (sourced by every script).

RL_MIN_TMUX_VERSION=301 # 3.1 (pane options は 3.1 から。indexed hooks は 3.0 から)

rl_get_option() { # <name> <default>
  local v
  v="$(tmux show-option -gqv "$1" 2>/dev/null)"
  printf '%s' "${v:-$2}"
}

rl_get_pane_option() { # <pane> <name>
  tmux show-options -pqv -t "$1" "$2" 2>/dev/null
}

rl_set_pane_option() { tmux set-option -p -t "$1" "$2" "$3"; }

rl_unset_pane_option() { tmux set-option -pu -t "$1" "$2" 2>/dev/null; }

rl_display() {
  [ "$(rl_get_option @rl_quiet off)" = "on" ] && return 0
  tmux display-message "tmux-robust-logging: $*" 2>/dev/null || true
}

rl_version_num() { # "3.3a" -> 303, "next-3.5" -> 305, パース不能(master等) -> 999
  local v major minor
  v="$(tmux display-message -p '#{version}' 2>/dev/null)"
  [ -n "$v" ] || v="$(tmux -V 2>/dev/null | cut -d' ' -f2)"
  v="${v#next-}"
  major="${v%%.*}"
  minor="${v#*.}"
  minor="${minor%%[!0-9]*}"
  case "$major" in ''|*[!0-9]*) echo 999; return ;; esac
  case "$minor" in ''|*[!0-9]*) minor=0 ;; esac
  echo $(( major * 100 + minor ))
}

rl_version_ok() { [ "$(rl_version_num)" -ge "$RL_MIN_TMUX_VERSION" ]; }

rl_shq() { # sh 用シングルクォート（パスに空白・括弧があっても安全に渡す）
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

rl_fmt_escape() { # tmux の format 展開に '#' を食われないようエスケープ
  printf '%s' "$1" | sed 's/#/##/g'
}

# set-hook に保存する文字列は発火時に tmux のコマンドパーサ（二重引用符レイヤ）を
# もう一度通るため、\ " $ をそのレイヤ用にエスケープする。
# 直接 run-shell / bind-key に argv として渡す文字列には適用しないこと。
rl_dq_escape() {
  printf '%s' "$1" | sed 's/[\\"$]/\\&/g'
}

rl_sanitize_component() { # セッション名/ウィンドウ名を unix/windows 両対応のファイル名部品に
  local s
  s="$(printf '%s' "$1" \
    | LC_ALL=C sed -e 's/[[:cntrl:]]/_/g' -e 's@[/\\:*?"<>|'\''`$&;() ]@_@g' \
    | LC_ALL=C cut -c1-60)"
  printf '%s' "${s:-x}"
}

rl_state_dir() { # tmux サーバ PID ごとの状態ディレクトリ（pane 消滅後の finalize 用）
  local pid base
  pid="$(tmux display-message -p '#{pid}' 2>/dev/null)"
  base="${TMPDIR:-/tmp}/tmux-robust-logging-$(id -u)"
  mkdir -p "$base/${pid:-0}" 2>/dev/null
  chmod 700 "$base" 2>/dev/null
  printf '%s' "$base/${pid:-0}"
}

rl_meta_file() { printf '%s/%s.meta' "$(rl_state_dir)" "${1#%}"; }

rl_meta_get() { # <meta_file> <key>
  [ -f "$1" ] || return 1
  sed -n "s/^$2=//p" "$1" | head -n 1
}

rl_expand_path() {
  case "$1" in
    '~') printf '%s' "$HOME" ;;
    '~/'*) printf '%s/%s' "$HOME" "${1#\~/}" ;;
    *) printf '%s' "$1" ;;
  esac
}

rl_log_dir() { # ログ保存先。書けない場合は TMPDIR 配下へフォールバック
  local dir
  dir="$(rl_expand_path "$(rl_get_option @rl_log_dir "$HOME/tmux-logs")")"
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir" 2>/dev/null && chmod 700 "$dir" 2>/dev/null
  fi
  if [ ! -d "$dir" ] || [ ! -w "$dir" ]; then
    dir="${TMPDIR:-/tmp}/tmux-logs-$(id -u)"
    mkdir -p "$dir" 2>/dev/null
    chmod 700 "$dir" 2>/dev/null
  fi
  printf '%s' "$dir"
}

rl_raw_dir() { # raw ログの保存先。@rl_raw_dir が未設定なら整形ログと同じ場所
  local dir
  dir="$(rl_expand_path "$(rl_get_option @rl_raw_dir '')")"
  if [ -z "$dir" ]; then
    rl_log_dir
    return 0
  fi
  if [ ! -d "$dir" ]; then
    mkdir -p "$dir" 2>/dev/null && chmod 700 "$dir" 2>/dev/null
  fi
  if [ ! -d "$dir" ] || [ ! -w "$dir" ]; then
    rl_log_dir # 書けなければ通常ディレクトリへフォールバック（ロギング自体は止めない）
    return 0
  fi
  printf '%s' "$dir"
}

rl_trim_trailing_blank() { # 末尾の空行・空白のみの行を落とす（backlog 取り込み用）
  LC_ALL=C awk '{ l[NR] = $0 }
    END { n = NR; while (n > 0 && l[n] ~ /^[[:space:]]*$/) n--
          for (i = 1; i <= n; i++) print l[i] }'
}

# raw を残すか (on/off)。@rl_keep_raw の明示設定が最優先。
# 未設定の場合、raw 専用ディレクトリ (@rl_raw_dir) を指定しているなら
# 「raw を残したい」意図とみなして on にする。
rl_keep_raw_now() {
  local k
  k="$(tmux show-option -gqv @rl_keep_raw 2>/dev/null)"
  if [ -n "$k" ]; then
    printf '%s' "$k"
    return 0
  fi
  if [ -n "$(rl_get_option @rl_raw_dir '')" ]; then
    printf 'on'
  else
    printf 'off'
  fi
}

rl_pane_exists() { tmux display-message -p -t "$1" '' >/dev/null 2>&1; }

# 状態は自前で追跡せず、常に tmux 本体の #{pane_pipe} を真実として使う
rl_pane_piped() { [ "$(tmux display-message -p -t "$1" '#{?pane_pipe,1,0}' 2>/dev/null)" = "1" ]; }

rl_base_name() { # <pane> -> "<dir>/tmux_<session>_<idx>-<win>_p<id>_<ts>"（拡張子なし）
  local pane="$1" dir sess idx win ts
  dir="$(rl_log_dir)"
  sess="$(rl_sanitize_component "$(tmux display-message -p -t "$pane" '#{session_name}')")"
  idx="$(tmux display-message -p -t "$pane" '#{window_index}')"
  win="$(rl_sanitize_component "$(tmux display-message -p -t "$pane" '#{window_name}')")"
  ts="$(date +%Y%m%dT%H%M%S)"
  printf '%s/tmux_%s_%s-%s_p%s_%s' "$dir" "$sess" "$idx" "$win" "${pane#%}" "$ts"
}

# mkdir ベースのロック。保持者の PID を記録し、保持者が死んでいれば
# ステイルロックとして壊す（SIGKILL された finalize が回収を永久に塞がないように）。
# attempts > 1 なら 0.2 秒間隔でリトライする（live 整形との短い競合を待つため）。
rl_acquire_lock() { # <lock_dir> [attempts=1] : 取得できたら 0
  local lock="$1" attempts="${2:-1}" holder i
  i=0
  while :; do
    if mkdir "$lock" 2>/dev/null; then
      printf '%s' "$$" > "$lock/pid" 2>/dev/null || true
      return 0
    fi
    holder="$(cat "$lock/pid" 2>/dev/null)" || holder=""
    case "$holder" in *[!0-9]*) holder="" ;; esac
    if [ -z "$holder" ]; then
      # pid 未記録: mkdir 直後で書き込み前の可能性があるため、
      # 1 分以上古いロックだけをステイル扱いにする
      if [ -n "$(find "$lock" -maxdepth 0 -mmin +1 2>/dev/null)" ]; then
        rm -rf "$lock" 2>/dev/null
      fi
    elif ! kill -0 "$holder" 2>/dev/null; then
      rm -rf "$lock" 2>/dev/null
    fi
    i=$((i+1))
    [ "$i" -ge "$attempts" ] && return 1
    sleep 0.2 2>/dev/null || sleep 1
  done
}

rl_release_lock() { rm -rf "$1" 2>/dev/null || true; }

rl_wait_quiesce() { # <base> : raw 出力の増加が止まるまで待つ（最大 ~2 秒）
  local base="$1" prev cur i
  prev="__none__"
  for i in 1 2 3 4 5 6 7 8 9 10; do
    cur="$(LC_ALL=C wc -c "$base".raw.log* 2>/dev/null | LC_ALL=C tail -n 1)"
    if [ -n "$cur" ] && [ "$cur" = "$prev" ]; then
      return 0
    fi
    prev="$cur"
    sleep 0.2 2>/dev/null || sleep 1
  done
  return 0
}
