#!/usr/bin/env bash
# ロギングの後始末: part 結合 -> sanitize -> raw 削除(オプション) -> 状態掃除。
# 使い方: finalize.sh <pane_id>   または   finalize.sh --sweep
# --sweep は現在のサーバの未 finalize メタに加え、既に死んだ過去サーバの
# PID ディレクトリも走査して回収する（kill-server / クラッシュ後のリカバリ）。
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

# 共通処理: part 結合 -> sanitize -> raw 削除。base と parts だけに依存し、
# ペインにもサーバにも触らない（死んだサーバのログ回収でも使えるように）。
rl_process_log() { # <base> <parts> [raw_base] [keep] [conv] [tsfmt]
  # keep は on/off、conv は charset 名 / "none" / 空。tsfmt は strftime / "none" / 空。
  local base="$1" parts="$2" raw_base="${3:-}" keep="${4:-}" conv_rec="${5:-}" tsfmt="${6:-}"
  local raw clean conv tsindex
  [ -n "$raw_base" ] || raw_base="$base" # 旧メタ互換: rawbase 未記録なら base と同じ
  [ -n "$keep" ] || keep="$(rl_keep_raw_now)" # 旧メタ互換: 現在の設定から判定
  if [ -n "$conv_rec" ]; then
    if [ "$conv_rec" = "none" ]; then conv=""; else conv="$conv_rec"; fi
  else
    conv="$(rl_get_option @rl_convert_from '')"
  fi
  [ "$tsfmt" = "none" ] && tsfmt=""
  raw="$raw_base.raw.log"
  tsindex="$raw_base.tsindex"
  rl_wait_quiesce "$raw_base" # 書き込みプロセスの排出を待つ

  if [ "${parts:-0}" = "1" ]; then
    # split の連番サフィックス(aa,ab,...)は辞書順 = 生成順
    cat "$raw.part-"* > "$raw" 2>/dev/null || true
    rm -f "$raw.part-"*
  fi

  if [ ! -s "$raw" ]; then
    rm -f "$raw" "$tsindex"
    return 0
  fi

  clean="$base.log"
  umask 077
  # sidecar があれば sanitize に日時を織り込ませる（環境変数で渡す）
  local rl_tsi=""
  [ -f "$tsindex" ] && rl_tsi="$tsindex"
  # sanitize の exit 0 かつ出力が空でないときだけ raw を消してよい
  if RL_ICONV_FROM="$conv" RL_TSINDEX="$rl_tsi" RL_TS_FORMAT="$tsfmt" \
       "$CURRENT_DIR/sanitize.sh" < "$raw" > "$clean" \
     && [ -s "$clean" ]; then
    if [ "$keep" != "on" ]; then
      rm -f "$raw" "$tsindex"
    fi
    rl_display "log saved: $clean"
  else
    rm -f "$clean"
    rl_display "sanitize 失敗または空の結果 - raw を残しました: $raw"
  fi
}

# 注意: ロックの解放に RETURN トラップは使わない。bash では内側の関数で設定した
# RETURN トラップが囲み関数の return 時にも再発火し、その間に別プロセスが取得した
# 同名ロックを壊してしまうため（rl_sweep からの呼び出しで実害が出る）。
rl_finalize_pane() { # <pane_id> : 現在のサーバのペインを finalize
  local pane="$1" meta lock
  meta="$(rl_meta_file "$pane")"
  # stop とフックの同時実行、および finalize 中の再 start と競合しないようロック。
  # live 整形（watchdog）が短時間ロックを持つことがあるためリトライ付きで待つ
  lock="$meta.lock"
  rl_acquire_lock "$lock" 25 || return 0
  rl_finalize_pane_locked "$pane" "$meta"
  rl_release_lock "$lock"
}

rl_finalize_pane_locked() { # <pane_id> <meta_file>
  local pane="$1" meta="$2" base parts rawbase keep conv tsfmt

  base="$(rl_get_pane_option "$pane" @rl_base)"
  parts="$(rl_get_pane_option "$pane" @rl_parts)"
  rawbase="$(rl_get_pane_option "$pane" @rl_rawbase)"
  keep="$(rl_get_pane_option "$pane" @rl_keep)"
  conv="$(rl_get_pane_option "$pane" @rl_conv)"
  tsfmt="$(rl_get_pane_option "$pane" @rl_tsfmt)"
  if [ -z "$base" ]; then
    base="$(rl_meta_get "$meta" base)" || base=""
    parts="$(rl_meta_get "$meta" parts)" || parts=""
    rawbase="$(rl_meta_get "$meta" rawbase)" || rawbase=""
  fi
  [ -n "$keep" ] || keep="$(rl_meta_get "$meta" keep)" || keep=""
  [ -n "$conv" ] || conv="$(rl_meta_get "$meta" conv)" || conv=""
  [ -n "$tsfmt" ] || tsfmt="$(rl_meta_get "$meta" tsfmt)" || tsfmt=""
  [ -n "$base" ] || return 0

  # ここから先、「自分が読んだ base のセッション」以外には一切触らない。
  # finalize 中に新しいロギングが始まっていた場合（@rl_base が別の値）に
  # 新セッションの pipe やメタ情報を壊さないため。
  if rl_pane_exists "$pane" \
     && [ "$(rl_get_pane_option "$pane" @rl_base)" = "$base" ] \
     && rl_pane_piped "$pane"; then
    tmux pipe-pane -t "$pane"
  fi

  rl_process_log "$base" "${parts:-0}" "$rawbase" "$keep" "$conv" "$tsfmt"

  if [ "$(rl_meta_get "$meta" base || true)" = "$base" ]; then
    rm -f "$meta" "$meta.live" "$meta.grow"
  fi
  if rl_pane_exists "$pane" \
     && [ "$(rl_get_pane_option "$pane" @rl_base)" = "$base" ]; then
    rl_unset_pane_option "$pane" @rl_base
    rl_unset_pane_option "$pane" @rl_parts
    rl_unset_pane_option "$pane" @rl_rawbase
    rl_unset_pane_option "$pane" @rl_keep
    rl_unset_pane_option "$pane" @rl_conv
    rl_unset_pane_option "$pane" @rl_tsfmt
  fi
}

rl_finalize_meta() { # <meta_file> : 死んだサーバのメタを回収（ペインには触らない）
  local meta="$1" base parts rawbase keep conv tsfmt lock
  [ -f "$meta" ] || return 0
  lock="$meta.lock"
  rl_acquire_lock "$lock" 5 || return 0
  base="$(rl_meta_get "$meta" base)" || base=""
  parts="$(rl_meta_get "$meta" parts)" || parts=""
  rawbase="$(rl_meta_get "$meta" rawbase)" || rawbase=""
  keep="$(rl_meta_get "$meta" keep)" || keep=""
  conv="$(rl_meta_get "$meta" conv)" || conv=""
  tsfmt="$(rl_meta_get "$meta" tsfmt)" || tsfmt=""
  if [ -n "$base" ]; then
    rl_process_log "$base" "${parts:-0}" "$rawbase" "$keep" "$conv" "$tsfmt"
  fi
  rm -f "$meta" "$meta.live" "$meta.grow"
  rl_release_lock "$lock"
}

rl_sweep() {
  local root cur_dir d m pid id pane comm wpid
  root="${TMPDIR:-/tmp}/tmux-robust-logging-$(id -u)"
  cur_dir="$(rl_state_dir)"

  # 1) 現在のサーバ: 生きて pipe 中のペインはスキップし、残りを回収
  for m in "$cur_dir"/*.meta; do
    [ -f "$m" ] || continue
    id="$(basename "$m" .meta)"
    pane="%$id"
    if rl_pane_exists "$pane" && rl_pane_piped "$pane"; then
      continue
    fi
    rl_finalize_pane "$pane"
  done

  # 2) 過去のサーバ (kill-server / クラッシュ後): メタファイルだけを頼りに回収。
  #    ペイン ID はサーバごとに %0 から振り直されるため、現在のペインとは突き合わせない。
  for d in "$root"/*/; do
    [ -d "$d" ] || continue
    d="${d%/}"
    [ "$d" = "$cur_dir" ] && continue
    pid="$(basename "$d")"
    case "$pid" in ''|*[!0-9]*) continue ;; esac
    if kill -0 "$pid" 2>/dev/null; then
      comm="$(ps -p "$pid" -o comm= 2>/dev/null)" || comm=""
      case "$comm" in
        '') continue ;;      # 判定できなければ安全側（生きているとみなして触らない）
        *tmux*) continue ;;  # 生きている別の tmux サーバ
      esac
    fi
    for m in "$d"/*.meta; do
      [ -f "$m" ] || continue
      rl_finalize_meta "$m"
    done
    # live 整形・暴走検知の状態ファイルと死んだウォッチドッグの pidfile を掃除
    # （rmdir を塞がないように）
    rm -f "$d"/*.live "$d"/*.grow 2>/dev/null
    if [ -f "$d/watchdog.pid" ]; then
      wpid="$(cat "$d/watchdog.pid" 2>/dev/null)" || wpid=""
      case "$wpid" in *[!0-9]*) wpid="" ;; esac
      if [ -z "$wpid" ] || ! kill -0 "$wpid" 2>/dev/null; then
        rm -f "$d/watchdog.pid"
      fi
    fi
    rmdir "$d" 2>/dev/null || true
  done
}

case "${1:-}" in
  --sweep) rl_sweep ;;
  ?*) rl_finalize_pane "$1" ;;
esac
