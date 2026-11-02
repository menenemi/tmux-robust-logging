#!/usr/bin/env bash
# ペインのロギングを開始する。まず raw（バイト完全一致）で取り、整形は停止時に行う。
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

main() {
  local pane base raw_base raw cmd parts max_mb meta n keep conv ts tsfmt tsindex
  pane="${1:-$(tmux display-message -p '#{pane_id}')}"
  rl_pane_exists "$pane" || exit 0

  if rl_pane_piped "$pane"; then
    rl_display "pane $pane は既に pipe されています（何もしません）"
    exit 0
  fi

  base="$(rl_base_name "$pane")"
  # raw は @rl_raw_dir が設定されていれば別ディレクトリへ（ファイル名は共通）
  raw_base="$(rl_raw_dir)/${base##*/}"
  # 同一秒に同一ペインで再開した場合の衝突回避。raw だけでなく整形済み .log や
  # split の part ファイルも含めた「この base の全成果物」で衝突を判定する
  n=0
  while { ls "$base".* || ls "$raw_base".*; } >/dev/null 2>&1 && [ "$n" -lt 5 ]; do
    n=$((n+1))
    base="${base}_$$$n"
    raw_base="${raw_base}_$$$n"
  done
  raw="$raw_base.raw.log"

  umask 077
  max_mb="$(rl_get_option @rl_max_size_mb 0)"
  case "$max_mb" in ''|*[!0-9]*) max_mb=0 ;; esac

  # raw を byte-exact に書きつつ「時刻→オフセット」の sidecar を取る。perl が必要。サイズ分割モード
  # とは併用しない（オフセットが part をまたぐため）。sidecar は @rl_timestamp に関係なく取る
  # （emaki が各コマンドの時刻と、入力と割り込んだ出力の境目を読む）。整形後の .log の行頭に日時を
  # 付けるかどうかだけを @rl_timestamp で決める（tsfmt が空なら付けない）。
  ts="off"; tsindex=""; tsfmt=""
  if command -v perl >/dev/null 2>&1; then
    ts="on"
    tsindex="$raw_base.tsindex"
  fi
  if [ "$(rl_get_option @rl_timestamp off)" = "on" ]; then
    if [ "$ts" = "on" ]; then
      tsfmt="$(rl_get_option @rl_timestamp_format '[%Y-%m-%d %H:%M:%S] ')"
    else
      rl_display "@rl_timestamp on には perl が必要です - 日時なしで記録します"
    fi
  fi

  parts=0
  if [ "$max_mb" -gt 0 ] && command -v split >/dev/null 2>&1; then
    parts=1
    ts="off"; tsindex=""; tsfmt="" # 分割モードでは日時は付けない
  fi

  # raw 保持・文字コード変換の判定は開始時に確定してメタに記録する。
  # サーバ死亡後の watchdog/sweep では tmux オプションが読めず、
  # 「@rl_keep_raw on なのに exit 時の finalize で raw が消える」事故になるため。
  keep="$(rl_keep_raw_now)"
  conv="$(rl_get_option @rl_convert_from '')"

  # emaki 用の索引は、ログのファイルを作る前に書く（ファイルだけがあって索引がまだ無い瞬間に、
  # ログ保存先を共有する別サーバの emaki が「索引の無い古いログ」と取り違えないように）
  rl_write_index "$pane" "$base" "$raw_base" "$parts" "${conv:-none}" "${tsfmt:-none}"

  if [ "$parts" = "1" ]; then
    # サイズ上限つき: split がローテーションしながら書く（part-aa, part-ab, ...）
    cmd="exec split -b ${max_mb}m - $(rl_shq "$raw.part-")"
  else
    if ! : >> "$raw"; then
      rl_remove_index "$base" "$raw_base"
      rl_display "$raw に書き込めません - ロギングを開始しませんでした"
      exit 1
    fi
    if [ "$ts" = "on" ]; then
      cmd="exec perl $(rl_shq "$CURRENT_DIR/rl_tee.pl") $(rl_shq "$raw") $(rl_shq "$tsindex")"
    else
      cmd="exec cat - >> $(rl_shq "$raw")"
    fi
  fi

  # pipe 開始前に既にペインへ出ていた画面・スクロールバックをログ先頭に取り込む。
  # ペイン誕生から pipe 確立までの数ミリ秒の隙間と、後からロギングを有効化した
  # 場合の過去分を救う（capture-pane のレンダリング結果なので raw バイトではない）
  # 末尾の空行（ペイン作成直後はほぼ画面高さぶんの空行になる）はトリムして取り込む
  if [ "$(rl_get_option @rl_capture_backlog off)" = "on" ]; then
    if [ "$parts" = "1" ]; then
      # part-0 は split の連番 (part-aa...) より辞書順で前に来るので結合順が保たれる
      tmux capture-pane -t "$pane" -epJS - 2>/dev/null | rl_trim_trailing_blank > "$raw.part-0" || true
      [ -s "$raw.part-0" ] || rm -f "$raw.part-0"
    else
      tmux capture-pane -t "$pane" -epJS - 2>/dev/null | rl_trim_trailing_blank >> "$raw" || true
    fi
  fi

  # pipe-pane はコマンド文字列を format 展開するため '#' をエスケープして渡す
  tmux pipe-pane -t "$pane" "$(rl_fmt_escape "$cmd")"

  # 自前の状態フラグではなく tmux 本体に開始できたかを確認する
  if ! rl_pane_piped "$pane"; then
    # rl_tee.pl は起動直後に tsindex へ初期レコードを書くため、失敗時は
    # raw と tsindex の両方を消す（この時点ではまだ meta 未書き込み = sweep で
    # 回収されないので、ここで消さないと tsindex が孤児として残る）。
    # $tsindex が空でも rm -f "" は無害な no-op。
    rm -f "$raw" "$tsindex"
    rl_remove_index "$base" "$raw_base"
    rl_display "pipe-pane の開始に失敗しました ($pane)"
    exit 1
  fi

  # メタ情報は pane option（高速路）と状態ファイル（pane 消滅後用）の両方に置く。
  # tsfmt は空だと meta の行が壊れる/後段の判定に困るので "none" を番兵にする。
  rl_set_pane_option "$pane" @rl_base "$base"
  rl_set_pane_option "$pane" @rl_parts "$parts"
  rl_set_pane_option "$pane" @rl_rawbase "$raw_base"
  rl_set_pane_option "$pane" @rl_keep "$keep"
  rl_set_pane_option "$pane" @rl_conv "${conv:-none}"
  rl_set_pane_option "$pane" @rl_tsfmt "${tsfmt:-none}"
  meta="$(rl_meta_file "$pane")"
  printf 'base=%s\nparts=%s\nrawbase=%s\nkeep=%s\nconv=%s\ntsfmt=%s\n' \
    "$base" "$parts" "$raw_base" "$keep" "${conv:-none}" "${tsfmt:-none}" > "$meta"

  rl_display "logging $pane -> $raw"
}

# emaki（セッション単位の HTML 出力）用の索引。ペインやサーバが消えた後でも「どのサーバ・
# セッション・ペインのログか」を確実に辿れるよう、ログ保存先の隠しディレクトリ .rlindex に
# 1 ログ 1 ファイルで残す（ログ本体の一覧を散らかさない）。失敗してもロギングには影響させない。
# raw を別ディレクトリ（@rl_raw_dir）に置くときはそちらの .rlindex にも置く。@rl_raw_dir を
# 共有する別サーバ（ログ保存先は別）の emaki が、raw だけを見て持ち主を取り違えないように。
rl_write_index() { # <pane> <base> <rawbase> <parts> <conv> <tsfmt>
  local pane="$1" body info us idx_dir tmp
  local i_pid i_sid i_sname i_screated i_widx i_wname i_pidx i_width
  local -a idx_dirs
  idx_dirs=("${2%/*}/.rlindex")
  [ -z "${3:-}" ] || [ "${3%/*}" = "${2%/*}" ] || idx_dirs+=("${3%/*}/.rlindex")
  us=$'\037'
  info="$(tmux display-message -p -t "$pane" "#{pid}${us}#{session_id}${us}#{session_name}${us}#{session_created}${us}#{window_index}${us}#{window_name}${us}#{pane_index}${us}#{pane_width}" 2>/dev/null)" || return 0
  IFS="$us" read -r i_pid i_sid i_sname i_screated i_widx i_wname i_pidx i_width <<EOF
$info
EOF
  body="$(
    printf 'version=1\n'
    printf 'server_pid=%s\nsession_id=%s\nsession_name=%s\nsession_created=%s\n' \
      "$i_pid" "$i_sid" "$i_sname" "$i_screated"
    printf 'window_index=%s\nwindow_name=%s\npane_index=%s\npane_id=%s\npane_width=%s\n' \
      "$i_widx" "$i_wname" "$i_pidx" "$pane" "$i_width"
    printf 'start=%s\nbase=%s\nrawbase=%s\nparts=%s\nconv=%s\ntsfmt=%s\n' \
      "$(date +%s)" "$2" "$3" "$4" "$5" "$6"
    printf 'host=%s\nuser=%s\n' "$(hostname 2>/dev/null)" "$(id -un 2>/dev/null)"
  )"
  for idx_dir in "${idx_dirs[@]}"; do
    mkdir -p "$idx_dir" 2>/dev/null || continue
    chmod 700 "$idx_dir" 2>/dev/null
    # 書きかけを読まれないよう、隠し一時ファイルに書き切ってから mv で置き換える
    tmp="$idx_dir/.${2##*/}.info.$$"
    if { printf '%s\n' "$body" > "$tmp"; } 2>/dev/null; then
      mv -f "$tmp" "$idx_dir/${2##*/}.info" 2>/dev/null || rm -f "$tmp" 2>/dev/null
    else
      rm -f "$tmp" 2>/dev/null
    fi
  done
  return 0
}

rl_remove_index() { # <base> <rawbase> : 開始できなかったログの索引を消す
  rm -f "${1%/*}/.rlindex/${1##*/}.info" "${2%/*}/.rlindex/${1##*/}.info" 2>/dev/null
  return 0
}

main "$@"
