#!/usr/bin/env bash
# tmux サーバの終了を待って finalize --sweep を実行するウォッチドッグ。
# サーバ内のフックは「サーバ自身が死ぬ瞬間」には信頼できないため、
# サーバの外（デタッチ済みプロセス）から死を検知して後始末する。
#
# さらに @rl_live_clean <秒> が設定されていれば、待機中に「出力が 1 サイクル
# （約 2 秒）静止した raw」から整形済み .log をその場で再生成する。
# コマンド終了 = 出力静止なので、実質コマンド実行のたびに .log が更新される。
# raw -> 整形は純粋な変換なので何度再生成しても安全（finalize が最終版を作る）。
#
# 使い方: watchdog.sh <server_pid> <state_dir>   (spawn_watchdog.sh から起動される)
set -u
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$CURRENT_DIR/helpers.sh"

server_pid="${1:-}"
state_dir="${2:-}"
case "$server_pid" in ''|*[!0-9]*) exit 0 ;; esac
[ -d "$state_dir" ] || exit 0

# 二重起動防止（spawn 側のチェックとの競合に備えて自分でも確認する）
pf="$state_dir/watchdog.pid"
old="$(cat "$pf" 2>/dev/null)" || old=""
case "$old" in *[!0-9]*) old="" ;; esac
if [ -n "$old" ] && [ "$old" != "$$" ] && kill -0 "$old" 2>/dev/null; then
  exit 0
fi
printf '%s' "$$" > "$pf" 2>/dev/null || exit 0

umask 077

rl_live_refresh() { # <interval> : 出力が静止した raw から .log を再生成
  local iv="$1" now m base rawbase parts conv raw sz prev done_sz t live tsfmt tsi
  now="$(date +%s)"
  for m in "$state_dir"/*.meta; do
    [ -f "$m" ] || continue
    parts="$(rl_meta_get "$m" parts)" || parts=0
    [ "${parts:-0}" = "1" ] && continue # サイズ分割モードは finalize 時のみ整形
    base="$(rl_meta_get "$m" base)" || continue
    [ -n "$base" ] || continue
    rawbase="$(rl_meta_get "$m" rawbase)" || rawbase=""
    [ -n "$rawbase" ] || rawbase="$base"
    raw="$rawbase.raw.log"
    [ -f "$raw" ] || continue
    sz="$(wc -c < "$raw" 2>/dev/null)" || continue
    live="$m.live" # 1行目=前サイクルのサイズ, 2行目=最後に整形したサイズ, 3行目=その時刻
    prev=""; done_sz=""; t=0
    if [ -f "$live" ]; then
      prev="$(sed -n '1p' "$live")"
      done_sz="$(sed -n '2p' "$live")"
      t="$(sed -n '3p' "$live")"
    fi
    case "$t" in ''|*[!0-9]*) t=0 ;; esac
    # 「サイズが 1 サイクル安定」かつ「前回整形時から変化」かつ「間隔を経過」で再生成
    if [ "$sz" = "$prev" ] && [ "$sz" != "$done_sz" ] && [ $((now - t)) -ge "$iv" ]; then
      if rl_acquire_lock "$m.lock" 1; then
        if [ -f "$m" ] && [ -f "$raw" ]; then
          conv="$(rl_meta_get "$m" conv)" || conv=""
          [ "$conv" = "none" ] && conv=""
          tsfmt="$(rl_meta_get "$m" tsfmt)" || tsfmt=""
          [ "$tsfmt" = "none" ] && tsfmt=""
          tsi=""
          [ -f "$rawbase.tsindex" ] && tsi="$rawbase.tsindex"
          if RL_ICONV_FROM="$conv" RL_TSINDEX="$tsi" RL_TS_FORMAT="$tsfmt" \
               "$CURRENT_DIR/sanitize.sh" < "$raw" > "$base.log.tmp" \
             && [ -s "$base.log.tmp" ]; then
            mv -f "$base.log.tmp" "$base.log" # 読者が中途半端な内容を見ないよう atomic に
          else
            rm -f "$base.log.tmp"
          fi
          done_sz="$sz"
          t="$now"
        fi
        rl_release_lock "$m.lock"
      fi
    fi
    printf '%s\n%s\n%s\n' "$sz" "$done_sz" "$t" > "$live" 2>/dev/null || true
  done
}

rl_growth_guard() { # raw の暴走成長を警告（自ペインで raw を cat したループ等）
  local thr m base rawbase raw sz gf prev streak lastwarn now
  thr="${RL_GROWTH_WARN_OVERRIDE:-$(rl_get_option @rl_growth_warn_mb 10)}"
  case "$thr" in ''|*[!0-9]*) thr=10 ;; esac
  [ "$thr" -gt 0 ] || return 0
  now="$(date +%s)"
  for m in "$state_dir"/*.meta; do
    [ -f "$m" ] || continue
    base="$(rl_meta_get "$m" base)" || continue
    rawbase="$(rl_meta_get "$m" rawbase)" || rawbase=""
    [ -n "$rawbase" ] || rawbase="$base"
    raw="$rawbase.raw.log"
    [ -f "$raw" ] || continue
    sz="$(wc -c < "$raw" 2>/dev/null)" || continue
    gf="$m.grow" # 1行目=前サイクルサイズ, 2行目=連続超過回数, 3行目=最終警告時刻
    prev=0; streak=0; lastwarn=0
    if [ -f "$gf" ]; then
      prev="$(sed -n '1p' "$gf")"
      streak="$(sed -n '2p' "$gf")"
      lastwarn="$(sed -n '3p' "$gf")"
    fi
    case "$prev" in ''|*[!0-9]*) prev=0 ;; esac
    case "$streak" in ''|*[!0-9]*) streak=0 ;; esac
    case "$lastwarn" in ''|*[!0-9]*) lastwarn=0 ;; esac
    if [ $((sz - prev)) -ge $((thr * 1024 * 1024)) ]; then
      streak=$((streak + 1))
    else
      streak=0
    fi
    if [ "$streak" -ge 3 ] && [ $((now - lastwarn)) -ge 60 ]; then
      rl_display "raw ログが毎秒数MB級で成長し続けています: $raw - ロギング中のペインで raw を cat/tail していませんか？（自己増殖ループの疑い）"
      lastwarn="$now"
      streak=0
    fi
    printf '%s\n%s\n%s\n' "$sz" "$streak" "$lastwarn" > "$gf" 2>/dev/null || true
  done
}

# サーバが生きている間は待機。PID 再利用に備え、可能ならプロセス名も確認する
while kill -0 "$server_pid" 2>/dev/null; do
  comm="$(ps -p "$server_pid" -o comm= 2>/dev/null)" || comm=""
  if [ -n "$comm" ]; then
    case "$comm" in *tmux*) ;; *) break ;; esac # PID が別プロセスに再利用された
  fi
  iv="${RL_LIVE_CLEAN_OVERRIDE:-$(rl_get_option @rl_live_clean 0)}"
  case "$iv" in ''|*[!0-9]*) iv=0 ;; esac
  if [ "$iv" -gt 0 ]; then
    rl_live_refresh "$iv"
  fi
  rl_growth_guard
  sleep 2
done

# サーバ死亡: pipe の書き込みプロセス (cat/split) の排出を少し待ってから回収
sleep 1
rm -f "$pf"
exec "$CURRENT_DIR/finalize.sh" --sweep
