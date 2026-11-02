# perl が無い環境向けのフォールバックサニタイザ（POSIX awk / LC_ALL=C で実行）。
# 行単位処理のため、行をまたぐ OSC/DCS シーケンスは完全には除去できない。
BEGIN {
  esc = sprintf("%c", 27)
  bel = sprintf("%c", 7)
  cr  = sprintf("%c", 13)
  osc  = esc "\\][^" bel esc "]*(" bel "|" esc "\\\\)?"
  dcs  = esc "[PX^_][^" esc "]*(" esc "\\\\)?"
  csi  = esc "\\[[0-9:;<=>?]*[ -/]*[@-~]"
  esc2 = esc "[ -/]*[0-~]"
  ctrl = ""
  for (i = 1; i <= 31; i++)
    if (i != 9) ctrl = ctrl sprintf("%c", i)
  ctrl = ctrl sprintf("%c", 127)
  ctrlre = "[" ctrl "]"
}
{
  line = $0
  gsub(osc, "", line)
  sub(esc "\\][^" bel esc "]*$", "", line)         # 行末で未終端の OSC
  gsub(dcs, "", line)
  gsub(csi, "", line)
  sub(esc "\\[[0-9:;<=>?]*[ -/]*$", "", line)      # 行末で未終端の CSI
  gsub(esc2, "", line)
  gsub(esc, "", line)
  sub("(" cr ")+$", "", line)      # CRLF の CR（行末）を先に落とす
  n = split(line, seg, cr)         # 行内 CR: 最後のセグメントだけ残す
  line = seg[n]
  gsub(ctrlre, "", line)           # BS 含む残りの制御文字を除去（消去の再現はしない）
  print line
}
