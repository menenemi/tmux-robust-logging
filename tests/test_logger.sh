#!/usr/bin/env bash
# rl_tee.pl records one sidecar line per read, and finalize.sh keeps .log free of dates when @rl_timestamp is off
# while a sidecar exists. No tmux needed: a stub answers show-option.
# usage: bash test_logger.sh <plugin dir>
set -u
P="$1"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
fail=0
ok() { echo "ok   $1"; }
ng() { echo "FAIL $1"; fail=1; }

mkdir "$T/bin"
printf '#!/bin/sh\nexit 1\n' > "$T/bin/tmux"
chmod +x "$T/bin/tmux"
export PATH="$T/bin:$PATH"

# --- rl_tee.pl: keys and output written at different moments stay separate reads
raw="$T/a.raw.log"
{ for c in g i t; do printf '%s' "$c"; sleep 0.2; done
  printf '[app] request handled\r\n'; sleep 0.2
  printf ' status\r\n'; } | perl "$P/scripts/rl_tee.pl" "$raw" "$T/a.tsindex"
if [ "$(cat "$raw")" = "$(printf 'git[app] request handled\r\n status\r\n')" ]; then ok "raw is byte exact"; else ng "raw"; fi
offs="$(awk '{print $2}' "$T/a.tsindex" | tr '\n' ' ')"
if [ "$offs" = "0 1 2 3 26 35 " ]; then ok "one sidecar record per read: $offs"; else ng "offsets: $offs"; fi

# --- finalize: same raw + sidecar, with and without @rl_timestamp
mklog() {  # <name>
  printf 'hello\r\nworld\r\n' > "$T/$1.raw.log"
  printf '1700000000 0\n1700000000 7\n1700000001 14\n' > "$T/$1.tsindex"
}
run() {  # <name> <tsfmt> <keep>
  ( set -u; . "$P/scripts/finalize.sh"; rl_process_log "$T/$1" 0 "$T/$1" "$3" none "$2" ) >/dev/null 2>&1
}
mklog off; run off none on
if [ "$(cat "$T/off.log")" = "$(printf 'hello\nworld')" ]; then ok "timestamp off: .log has no dates"; else ng "off: $(cat "$T/off.log")"; fi
if [ -f "$T/off.tsindex" ] && [ -f "$T/off.raw.log" ]; then ok "keep on: raw and sidecar stay"; else ng "keep on"; fi
mklog on; run on '[%H:%M:%S] ' on
if grep -q '^\[[0-9:]*\] hello' "$T/on.log"; then ok "timestamp on: .log has dates"; else ng "on: $(cat "$T/on.log")"; fi
mklog old; run old '' on
if grep -q '^\[.*\] hello' "$T/old.log"; then ok "old meta without tsfmt: dates as before"; else ng "old: $(cat "$T/old.log")"; fi
mklog gone; run gone none off
if [ ! -e "$T/gone.tsindex" ] && [ ! -e "$T/gone.raw.log" ] && [ -s "$T/gone.log" ]; then ok "keep off: raw and sidecar removed"; else ng "keep off"; fi
exit $fail
