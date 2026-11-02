"""端末出力の 1 論理行を、色付きの run 列にレンダリングする。

scripts/sanitize.pl の行レンダラと同じ端末モデル（CR / BS / 行内編集 CSI /
CUU・CUD の縦移動 / 全角 2 桁 / 巨大パラメータのクランプ）に、次の 2 つを足したもの:

- SGR（色・太字・下線など）をセルごとの属性として保持し、同じ属性の連続を run にまとめる
- イベントの記録: ESC[?2004h（zle/readline が入力を受け付け始めた位置 = プロンプトの末尾）と
  OSC 133;B（シェル統合のプロンプト末尾マーク）のカーソル位置

sanitize.pl は日々のログ整形に使われているので挙動を変えないよう触らず、こちらは独立実装。
"""

import bisect
import re
import unicodedata

F_BOLD, F_DIM, F_ITALIC, F_UNDER = 1, 2, 4, 8
F_INV, F_HIDDEN, F_STRIKE = 32, 64, 128

# 1 行がこれを超えたら全部はレンダリングしない（巨大バイナリの cat などへの備え）
MAX_LINE = 2 * 1024 * 1024
KEEP_LINE = 1024 * 1024


class StyleTable:
    """(fg, bg, flags) の組を ID に割り当てる。HTML 側ではこの ID が CSS クラスになる。"""

    def __init__(self):
        self.ids = {("", "", 0): 0}
        self.list = [("", "", 0)]
        # 行末の空白として切り詰めてよいか（背景色・反転・下線・取消線が無いスタイル）
        self.blank = [True]

    def sid(self, st):
        key = (st[0], st[1], st[2])
        i = self.ids.get(key)
        if i is None:
            i = len(self.list)
            self.ids[key] = i
            self.list.append(key)
            self.blank.append(st[1] == "" and not (st[2] & (F_INV | F_UNDER | F_STRIKE)))
        return i

    def to_json(self):
        out = []
        for fg, bg, f in self.list:
            d = {}
            if fg:
                d["fg"] = fg
            if bg:
                d["bg"] = bg
            if f:
                d["f"] = f
            out.append(d)
        return out


def _pal256(v):
    if v is None or not v.isdigit() or len(v) > 3:
        return None
    n = int(v)
    if n > 255:
        return None
    # 0〜15 番でもアプリが送ったとおり 30〜37 / 90〜97 の色（p）とは分けて残す。なお tmux は 0〜7 番を
    # 30〜37 にして端末へ送るので、QTerminal では太字のとき 30〜37 と同じく明るい色になる（HTML 側で合わせる）
    return "x%d" % n


def _rgb(vals):
    if len(vals) != 3:
        return None
    try:
        r, g, b = (int(x) for x in vals)
    except ValueError:
        return None
    if not all(0 <= x <= 255 for x in (r, g, b)):
        return None
    return "#%02x%02x%02x" % (r, g, b)


def apply_sgr(st, ps):
    """SGR パラメータを状態 st = [fg, bg, flags] に適用する（; 形式と 38:2::r:g:b 形式の両対応）。"""
    if ps == "":
        ps = "0"
    groups = ps.split(";")
    n = len(groups)
    i = 0
    while i < n:
        sub = groups[i].split(":")
        c = sub[0] or "0"
        if not c.isdigit():
            i += 1
            continue
        # 桁数の多すぎる値（int() が 4300 桁超で例外を投げる）は無効なパラメータとして読み飛ばす
        c = int(c) if len(c) <= 4 else -1
        if c in (38, 48, 58):
            col = None
            if len(sub) > 1:  # コロン形式
                if sub[1] == "5" and len(sub) > 2:
                    col = _pal256(sub[2])
                elif sub[1] == "2" and len(sub) >= 5:
                    col = _rgb(sub[-3:])
                i += 1
            else:  # セミコロン形式
                mode = groups[i + 1] if i + 1 < n else ""
                if mode == "5":
                    col = _pal256(groups[i + 2] if i + 2 < n else None)
                    i += 3
                elif mode == "2":
                    col = _rgb(groups[i + 2:i + 5])
                    i += 5
                else:
                    i += 2
            if col is not None:
                if c == 38:
                    st[0] = col
                elif c == 48:
                    st[1] = col
            continue
        if c == 0:
            st[0], st[1], st[2] = "", "", 0
        elif c == 1:
            st[2] |= F_BOLD
        elif c == 2:
            st[2] |= F_DIM
        elif c == 3:
            st[2] |= F_ITALIC
        elif c == 4:
            if len(sub) > 1 and sub[1] == "0":
                st[2] &= ~F_UNDER
            else:
                st[2] |= F_UNDER
        elif c == 7:
            st[2] |= F_INV
        elif c == 8:
            st[2] |= F_HIDDEN
        elif c == 9:
            st[2] |= F_STRIKE
        elif c == 21:
            st[2] |= F_UNDER
        elif c == 22:
            st[2] &= ~(F_BOLD | F_DIM)
        elif c == 23:
            st[2] &= ~F_ITALIC
        elif c == 24:
            st[2] &= ~F_UNDER
        elif c == 27:
            st[2] &= ~F_INV
        elif c == 28:
            st[2] &= ~F_HIDDEN
        elif c == 29:
            st[2] &= ~F_STRIKE
        elif 30 <= c <= 37:
            st[0] = "p%d" % (c - 30)
        elif c == 39:
            st[0] = ""
        elif 40 <= c <= 47:
            st[1] = "p%d" % (c - 40)
        elif c == 49:
            st[1] = ""
        elif 90 <= c <= 97:
            st[0] = "p%d" % (c - 82)
        elif 100 <= c <= 107:
            st[1] = "p%d" % (c - 92)
        i += 1
    st[2] &= 0xFF


_wcache = {}


def char_width(ch):
    """端末上のセル幅。全角・絵文字は 2、結合文字・ゼロ幅は 0、それ以外は 1。"""
    w = _wcache.get(ch)
    if w is None:
        o = ord(ch)
        if o < 0x300:
            w = 1
        elif unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf") \
                or 0xFE00 <= o <= 0xFE0F:
            w = 0
        elif unicodedata.east_asian_width(ch) in ("W", "F"):
            w = 2
        else:
            w = 1
        _wcache[ch] = w
    return w


def text_width(t):
    if t.isascii():
        return len(t)
    return sum(char_width(c) for c in t)


def cells_to_index(text, col):
    """セル位置 col を、レンダリング済み行文字列のインデックスに変換する。"""
    if text.isascii():
        return min(col, len(text))
    pos = 0
    for i, ch in enumerate(text):
        if pos >= col:
            return i
        pos += char_width(ch)
    return len(text)


_TOKEN = re.compile(
    r"(\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e])"   # 1 完全な CSI
    r"|(\x1b\]133;B(?:\x07|\x1b\\))"                  # 2 OSC 133;B（行内に残したもの）
    r"|(\x1b\[[\x30-\x3f]*[\x20-\x2f]*\Z)"            # 3 行末で切れた CSI
    r"|(\x1b[\x20-\x2f]+[\x30-\x7e])"                 # 4 文字集合指定など
    r"|(\x1b[\x30-\x7e]?)"                            # 5 2 バイト ESC / はぐれ ESC
    r"|([^\x00-\x1f\x7f\x1b]+)"                        # 6 文字の並び
    r"|([\x00-\x1f\x7f])",                             # 7 制御文字 1 つ
    re.S,
)
_CTRL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")       # TAB 以外の制御文字（ESC 含む）
_MOVES = re.compile(r"[\x08\r]|\x1b\[[0-9;]*[@ABCDGJKPX]|\x1bM")
_SGR = re.compile(r"\x1b\[([0-9;:]*)m\Z")
_CSI = re.compile(r"\x1b\[([\x30-\x3f]*)([\x20-\x2f]*)([\x40-\x7e])\Z")
_SGR_PS = re.compile(r"[0-9;:]*\Z")
_EDIT_PS = re.compile(r"[0-9;]*\Z")
_LEADNUM = re.compile(r"(\d+)")


def render_line(s, tbl, width=None):
    """1 論理行（\\n を含まない文字列）をレンダリングする。

    width を与えると端末の自動折り返し（DECAWM）もエミュレートする。zle は長い入力行で
    折り返しを前提にカーソルを動かす（オートサジェストの消去など）ので、入力行だけはこれが要る。

    戻り値: (rows, ev, trunc)
      rows  = [[(text, sid), ...], ...]  画面上の行（縦移動・折り返しがあれば複数）
      ev    = {"in": (row, col), "b": (row, col), "acc": (row, col), "stale": bool,
               "post": {row, ...}, "wrap": {row: cells}, "up": bool}
              acc  は入力確定（ESC[?2004l）の位置
              post は入力確定の後に文字が書かれた行 = コマンドの出力側
              wrap は自動折り返しで次の段へ進んだ行と、その時点で埋まっていたセル数
              up   はカーソルを上へ動かした（zle が補完の一覧・履歴検索の表示の後で入力行へ戻るなど）
              rp   は右プロンプトの (row, col)。入力受付の印がある行だけ（None = 右プロンプトは無い）
              lclamp は文字を書く前に左端より左へ戻ろうとした桁数（zle が、割り込んだ出力で次の行へ
                     押し出されたことを知らずに、打った入力の頭へ戻って描き直した）
      trunc = 省略した文字数
    """
    trunc = 0
    if len(s) > MAX_LINE:
        lcr = s.rfind("\r")
        trunc = len(s) - KEEP_LINE
        if lcr >= 0 and lcr > len(s) - KEEP_LINE:
            s = s[-KEEP_LINE:]  # プログレスバー等は最後の CR 以降が最終表示なので末尾を残す
        else:
            s = s[:KEEP_LINE]
    ev = {}
    if not _CTRL.search(s):  # 高速路: 制御文字も ESC も無い
        t = s.rstrip(" ")
        return ([[(t, 0)]] if t else [[]]), ev, trunc
    s = _drop_status_draws(s)
    if width or _MOVES.search(s):
        rows = _render_full(s, tbl, ev, width)
        if rows is not None:
            return rows, ev, trunc
        # 行内編集が異常に多い行（攻撃的な出力など）は、時間がかかり過ぎないよう簡易描画に落とす
        ev.clear()
    return _render_linear(s, tbl, ev), ev, trunc


_SC_RC = re.compile(r"\x1b[78]")
_ABS_POS = re.compile(r"\x1b\[[0-9;]*[Hf]")


def _drop_status_draws(s):
    """カーソルを保存（ESC 7）→ 絶対位置へ移動して描く → 復帰（ESC 8）の区間を取り除く。
    apt の進捗バーのように、画面の最下行など別の場所に描く表示が、出力の行にくっつかないように。"""
    if "\x1b7" not in s or "\x1b8" not in s:
        return s
    cups = [m.start() for m in _ABS_POS.finditer(s)]
    if not cups:
        return s
    out = []
    last = 0
    at = None
    for m in _SC_RC.finditer(s):
        if m.group() == "\x1b7":
            at = m.start()
        elif at is not None:
            i = bisect.bisect_left(cups, at)
            if i < len(cups) and cups[i] < m.start():
                out.append(s[last:at])
                last = m.end()
            at = None
    if not out:
        return s
    out.append(s[last:])
    return "".join(out)


def _render_linear(s, tbl, ev):
    """カーソル移動の無い行（ls --color 等）。列は文字幅の累積で追う。"""
    st = ["", "", 0]
    sid = 0
    runs = []
    col = 0
    for m in _TOKEN.finditer(s):
        g = m.lastindex
        if g == 1:
            seq = m.group(1)
            mm = _SGR.match(seq)
            if mm:
                apply_sgr(st, mm.group(1))
                sid = tbl.sid(st)
            elif seq == "\x1b[?2004h":
                ev["in"] = (0, col)
                ev["rp"] = None  # カーソル移動が無ければ、入力位置より右には何も描かれていない
            elif seq == "\x1b[?2004l":
                ev["acc"] = (0, col)
            continue
        if g == 2:
            ev["b"] = (0, col)
            continue
        if g == 6:
            t = m.group(6)
            col += text_width(t)
        elif g == 7 and m.group(7) == "\t":
            t = "\t"
            col += 1
        else:
            continue
        if runs and runs[-1][1] == sid:
            runs[-1][0].append(t)
        else:
            runs.append([[t], sid])
    out = [["".join(p), si] for p, si in runs]
    while out and tbl.blank[out[-1][1]]:
        out[-1][0] = out[-1][0].rstrip(" ")
        if out[-1][0]:
            break
        out.pop()
    return [[(t, si) for t, si in out]]


def _row(rows, r):
    while len(rows) <= r:
        rows.append(None)
    if rows[r] is None:
        rows[r] = [[], []]
    return rows[r][0], rows[r][1]


def _norm_cut(c, sd, pos):
    """位置 pos での縦の切断が全角文字をまたぐなら、その文字全体を空白にする（端末と同じ）。"""
    if 0 < pos < len(c) and c[pos] == "":
        c[pos - 1] = " "
        sd[pos - 1] = 0
        c[pos] = " "
        sd[pos] = 0


def _right_part(c, sd, k, blank):
    """列 k から右に見えている部分 → (開始列, その列から最後に見えている列までのセル)。無ければ None。"""
    n = len(c)
    j = k
    while j < n and (c[j] is None or c[j] == "" or (c[j] == " " and blank[sd[j]])):
        j += 1
    if j >= n:
        return None
    end = n
    while end > j and (c[end - 1] is None or (c[end - 1] == " " and blank[sd[end - 1]])):
        end -= 1
    return j, tuple(c[j:end])


def _rp_close(run, rows, r, end, W):
    """入力受付の印の直後に右へ飛んで書いた部分（run = (row, 開始列)、今の行 r の end まで書いた）→ 折り返さずに
    右端まで届いていれば右プロンプトの候補 (row, 開始列, セル)。そうでなければ None。"""
    if not W or r != run[0] or end < W - 4:
        return None
    return run[0], run[1], tuple(rows[r][0][run[1]:end])


def _render_full(s, tbl, ev, width=None):
    """カーソル移動のある行。行内編集の手間（セルの書き換え量）が入力長に比べて極端に多い場合は
    None を返す（呼び出し側が簡易描画に切り替える。何 MB もある攻撃的な行で固まらないため）。"""
    rows = [[[], []]]
    r = 0
    c, sd = rows[0]
    cur = 0
    st = ["", "", 0]
    sid = 0
    in_pos = None
    rp_at = None
    # 入力受付の印の後で zle が右プロンプトを描く形（印の直後に右端近くへ飛んで描き、入力位置へ戻る）:
    # rpj 0 = 印の前 / 1 = 印の後でまだ何も書いていない / 2 = そこから右へ飛んだ / 3 = 飛んだ先で書いている /
    # -1 = 終わり（右プロンプトを描いた・別の文字を書いた）
    rpj = 0
    rp_run = None
    rp_post = None
    wrote = False  # 文字を書いた（それより前に左端より左へ戻ろうとした量が lclamp）
    after_input = False
    post = set()
    wraps = {}
    W = width if width and width >= 8 else None
    budget = 32 * len(s) + 200000  # セル操作の上限

    for m in _TOKEN.finditer(s):
        g = m.lastindex
        if g == 1:
            ps, im, f = _CSI.match(m.group(1)).groups()
            if f == "m" and not im and _SGR_PS.match(ps):
                apply_sgr(st, ps)
                sid = tbl.sid(st)
                continue
            if ps == "?2004" and not im and f in "hl":
                if f == "h":
                    in_pos = (r, min(cur, W - 1) if W else cur)
                    ev["in"] = in_pos
                    ev.pop("stale", None)
                    after_input = False
                    # zle は右プロンプトを入力受付の印より前に描く。印の時点で入力位置より右に見えているもの
                    rp_at = _right_part(c, sd, in_pos[1], tbl.blank)
                    if rp_at is not None:
                        rp_at = (r,) + rp_at
                    rpj, rp_run, rp_post = 1, None, None
                else:
                    ev["acc"] = (r, cur)
                    if in_pos is not None:
                        after_input = True
                continue
            if im or f not in "@ABCDGJKPX" or not _EDIT_PS.match(ps):
                continue
            mnum = _LEADNUM.match(ps)
            if mnum:
                d = mnum.group(1)
                n = int(d) if len(d) <= 9 else 10 ** 9  # どのみち後でクランプされる
            else:
                n = -1
            if rpj == 3:
                rp_post, rpj = _rp_close(rp_run, rows, r, cur, W), -1
            elif rpj in (1, 2):
                rpj = 2 if f == "C" else 1
            if W and cur >= W:
                cur = W - 1  # 折り返し保留中（右端に書いた直後）の位置は右端
            if W and f in "CG":
                n = min(n, W)
            if f == "A":
                k = 1 if n < 1 else n
                r = r - k if r > k else 0
                c, sd = _row(rows, r)
                ev["up"] = True
            elif f == "B":
                k = 1 if n < 1 else min(n, 500)
                r = min(r + k, 500)
                c, sd = _row(rows, r)
            elif f == "D":
                k = 1 if n < 1 else n
                if k > cur and not wrote:
                    ev["lclamp"] = ev.get("lclamp", 0) + k - cur
                cur = max(cur - k, 0)
            elif f == "C":
                k = 1 if n < 1 else min(n, 10000)
                lim = max(len(c), 10000)
                if cur > lim:
                    lim = cur
                cur = min(cur + k, lim)  # 累積位置もクランプ（[10000C の繰り返し対策）
                if W:
                    cur = min(cur, W - 1)
            elif f == "G":
                k = 1 if n < 1 else n
                cur = min(k - 1, max(len(c), 10000))
                if W:
                    cur = min(cur, W - 1)
            elif f == "J":
                if n < 1:
                    _norm_cut(c, sd, cur)
                    del c[cur:]
                    del sd[cur:]
                    del rows[r + 1:]
            elif f == "K":
                mode = 0 if n < 0 else n
                if mode == 0:
                    _norm_cut(c, sd, cur)
                    del c[cur:]
                    del sd[cur:]
                elif mode == 1:
                    end = min(cur, len(c) - 1)
                    if end >= 0:
                        budget -= end + 1
                        c[:end + 1] = [" "] * (end + 1)
                        sd[:end + 1] = [0] * (end + 1)
                    _norm_cut(c, sd, end + 1)
                elif mode == 2:
                    del c[:]
                    del sd[:]
                # 範囲外パラメータは実端末と同じく無視
            elif f == "X":
                k = 1 if n < 1 else n
                _norm_cut(c, sd, cur)
                _norm_cut(c, sd, cur + k)
                hi = min(cur + k, len(c))
                if hi > cur:
                    budget -= hi - cur
                    c[cur:hi] = [" "] * (hi - cur)
                    sd[cur:hi] = [0] * (hi - cur)
            elif f == "P":
                k = 1 if n < 1 else n
                _norm_cut(c, sd, cur)
                _norm_cut(c, sd, cur + k)
                if cur < len(c):
                    budget -= len(c) - cur
                    del c[cur:cur + k]
                    del sd[cur:cur + k]
            elif f == "@":
                k = 1 if n < 1 else min(n, 10000)
                if len(c) + k > 20000:
                    k = max(20000 - len(c), 0)
                _norm_cut(c, sd, cur)
                if k > 0 and cur < len(c):
                    budget -= len(c) - cur + k
                    c[cur:cur] = [" "] * k
                    sd[cur:cur] = [0] * k
                    if W and len(c) > W:  # 右端からはみ出したセルは端末と同じく消える
                        _norm_cut(c, sd, W)
                        del c[W:]
                        del sd[W:]
            if budget < 0:
                return None
            continue
        if g == 2:
            ev["b"] = (r, cur)
            continue
        if (g == 5 and m.group(5) == "\x1bM") or (g == 7 and m.group(7) in "\r\x08"):
            if rpj == 3:
                rp_post, rpj = _rp_close(rp_run, rows, r, cur, W), -1
            elif rpj == 2:
                rpj = 1
        if g == 5 and m.group(5) == "\x1bM":
            # RI（逆改行）。tmux の中の TERM（screen / tmux 系）では terminfo の cuu1 がこれなので、
            # zle は 1 行上へ戻るのに ESC[A ではなくこれを使う
            if W and cur >= W:
                cur = W - 1
            r = r - 1 if r > 0 else 0
            c, sd = _row(rows, r)
            ev["up"] = True
            continue
        if g in (3, 4, 5):
            continue
        if g == 7:
            t = m.group(7)
            if t == "\r":
                cur = 0
                continue
            if t == "\x08":
                if W and cur >= W:
                    cur = W - 1
                if cur > 0:
                    cur -= 1
                elif not wrote:
                    ev["lclamp"] = ev.get("lclamp", 0) + 1
                continue
            if t != "\t":
                continue  # カーソルを動かさない制御文字はセルを使わない
            chars = "\t"
        else:
            chars = m.group(6)

        wrote = True
        if rpj == 2 and r == in_pos[0] and cur > in_pos[1]:
            rp_run, rpj = (r, cur), 3
        elif rpj in (1, 2):
            rpj = -1
        for ch in chars:
            w = 1 if ch == "\t" else char_width(ch)
            if w == 0:  # 結合文字は直前のセルに付ける（1 セルに付ける数には上限）
                p = cur - 1
                if 0 <= p < len(c) and c[p] == "":
                    p -= 1
                if 0 <= p < len(c) and c[p] and len(c[p]) < 16:
                    c[p] = c[p] + ch
                continue
            if W and cur + w > W:  # 自動折り返し（その時点で埋まっていたセル数を覚えておく）
                wraps[r] = min(cur, W)
                r += 1
                c, sd = _row(rows, r)
                cur = 0
            if after_input:
                post.add(r)
            if in_pos is not None and r == in_pos[0] and cur < in_pos[1]:
                # 入力開始位置より左が書き換えられた（プロンプトの再描画など）
                ev["stale"] = True
            if 0 < cur < len(c) and c[cur] == "":
                c[cur - 1] = " "
                sd[cur - 1] = 0
            last = cur + w - 1
            if last + 1 < len(c) and c[last] is not None and c[last] != "" and c[last + 1] == "":
                c[last + 1] = " "
                sd[last + 1] = 0
            need = cur + w
            if len(c) < need:
                # カーソル移動で離れた位置に書くと間を空白で埋める。その量も手間に数える
                # （ESC[10000C や ESC[B を繰り返すだけの短い行で巨大な出力にならないように）
                budget -= need - len(c)
                if budget < 0:
                    return None
                c.extend([None] * (need - len(c)))
                sd.extend([0] * (need - len(sd)))
            c[cur] = ch
            sd[cur] = sid
            if w == 2:
                c[cur + 1] = ""
                sd[cur + 1] = sid
            cur += w

    if post:
        ev["post"] = post
    if wraps:
        ev["wrap"] = wraps
    if rpj == 3:
        rp_post = _rp_close(rp_run, rows, r, cur, W)
    if in_pos is not None:
        # 右プロンプト: 入力受付の印の時点で入力位置より右に見えていて（印の直後に右端へ飛んで描いたものも）、
        # 最後までそのまま残ったもの（入力が伸びて zle が消した・割り込んだ出力が上書きしたなら無い）。
        # None = この行に右プロンプトは無い
        ev["rp"] = None
        if rp_at is None:
            rp_at = rp_post
        if rp_at is not None:
            rr, j, snap = rp_at
            if rr < len(rows) and rows[rr] is not None:
                cc, ss = rows[rr]
                if tuple(cc[j:j + len(snap)]) == snap and _right_part(cc, ss, j + len(snap), tbl.blank) is None:
                    ev["rp"] = (rr, j)
    # 内容のある最後の行まで出す（移動だけで何も書かれなかった下の行は出さない）
    lastrow = len(rows) - 1
    while lastrow > 0 and (rows[lastrow] is None or not rows[lastrow][0]):
        lastrow -= 1
    out = []
    blank = tbl.blank
    for i in range(lastrow + 1):
        row = rows[i] or [[], []]
        c, sd = row
        end = len(c) - 1
        while end >= 0:
            ch = c[end]
            if (ch is None or ch == " " or ch == "") and blank[sd[end]]:
                end -= 1
            else:
                break
        runs = []
        for j in range(end + 1):
            ch = c[j]
            if ch == "":
                continue  # 全角の右半分マーカー
            if ch is None:
                ch, si = " ", 0
            else:
                si = sd[j]
            if runs and runs[-1][1] == si:
                runs[-1][0].append(ch)
            else:
                runs.append([[ch], si])
        out.append([("".join(p), si) for p, si in runs])
    return out
