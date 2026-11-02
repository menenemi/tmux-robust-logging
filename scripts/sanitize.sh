#!/usr/bin/env bash
# stdin -> stdout: エスケープシーケンス除去、CR/BS 上書きの再現、CRLF -> LF 正規化。
# バイト単位で処理するためエンコーディングを問わない（CP932/UTF-16 の残骸も壊さない）。
# RL_ICONV_FROM=<charset> を与えると最後に iconv で UTF-8 へ変換する（不正バイトは捨てる）。
#
# 終了コードの契約: フィルタ本体が失敗したら必ず非 0 を返す（finalize が raw を温存する）。
# iconv は「不正バイトを捨てただけでも exit 1 を返す」ため、変換の成否は
# 終了コードではなく出力が得られたかどうかで判定し、失敗時は未変換で出力する。
set -u -o pipefail
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rl_filter() {
  if command -v perl >/dev/null 2>&1; then
    perl "$CURRENT_DIR/sanitize.pl"
  else
    # awk フォールバックは行単位のため、行をまたぐ OSC/DCS は完全には消えない。
    # awk は NUL を扱えない（レコード切断や無視をする実装がある）ので tr で先に落とす。
    LC_ALL=C tr -d '\000' | LC_ALL=C awk -f "$CURRENT_DIR/sanitize.awk"
  fi
}

tmp="$(mktemp "${TMPDIR:-/tmp}/rl-sanitize.XXXXXX")" || exit 1
cnv="$tmp.cnv"
trap 'rm -f "$tmp" "$cnv"' EXIT

if ! rl_filter > "$tmp"; then
  exit 1 # フィルタのクラッシュや途中終了を絶対に握りつぶさない
fi

if [ -n "${RL_ICONV_FROM:-}" ] && command -v iconv >/dev/null 2>&1; then
  iconv -f "$RL_ICONV_FROM" -t UTF-8 -c < "$tmp" > "$cnv" 2>/dev/null || true
  if [ -s "$cnv" ]; then
    cat "$cnv"
    exit 0
  fi
  # 変換が完全に失敗（不明な charset 等）: 未変換のまま出力にフォールバック
fi

cat "$tmp"
