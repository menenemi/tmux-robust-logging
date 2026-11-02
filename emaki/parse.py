"""raw ログ（pipe-pane のバイト列そのまま）を論理行に分解する。

- \\n で区切った論理行ごとに、raw 上のバイト範囲（= 時刻 sidecar と対応するオフセット）を持つ
- シェル統合の印（OSC 133 A/B/C/D, OSC 633;E）を拾う。印にはそのシェルだけが知る合言葉
  （;emaki=<nonce>）が付いていて、手元の記録（nonce registry）にあるものだけを信用する。
  コマンドの出力（攻撃対象のサーバが返す文字列など）に混ざった偽の印は、見えない制御列として捨てる。
  OSC 7（カレントディレクトリ）は合言葉を付けられないので、本物の A の直前にあるものだけを使う
- C と D は行の途中にあってもそこで行を割る。zsh の PROMPT_SP（改行で終わらなかった出力の直後に
  出る空白の列）も同様に割る（出力の最後の行がプロンプトに上書きされて消えるのを防ぐ）
- 全画面アプリ（vim / less / htop など、代替画面 ESC[?1049h〜l）の区間は 1 行のプレースホルダにする。
  終わりの印が来ないまま接続が切れた場合でも後続を飲み込まないよう、シェルに制御が戻った印でも閉じる
- DCS / tmux パススルー / その他の OSC は捨てる
"""

import bisect
import re

from .render import char_width, render_line, text_width

# zsh の PROMPT_SP: プロンプトの前に「PROMPT_EOL_MARK + (端末幅 - マーク幅) 個の空白 + CR +
# マーク幅ぶんの空白 + CR」を出す（tmux は xenl を持つので空白は幅ちょうど）。ここから、その
# プロンプト時点の端末幅が正確に分かる。（SGR の並びで指数的に遅くならない形にしてある）
_SGRS = r"(?:\x1b\[[0-9;]*m)*"
_PROMPT_SP = re.compile(
    r"^" + _SGRS + r"(?:(?P<mk>[^\s\x1b])" + _SGRS + r")?(?P<sp> {8,1000})\r(?P<w> {0,2})\r")

_SPLIT = re.compile(
    rb"(?P<nl>\n)"
    rb"|(?P<dd>\x1b\x1b)"
    # DCS / SOS / PM / APC と、screen・tmux のタイトル（ESC k ... ESC \。oh-my-zsh が tmux の中で毎回出す）
    rb"|(?P<dcs>\x1b[PX^_k](?:\x1b\x1b|[^\x1b\n])*(?:\x1b\\|(?=\x1b)|(?=\n)|\Z))"
    rb"|(?P<osc>\x1b\](?P<num>[0-9]*)(?:;(?P<pl>[^\x07\x1b\n]*))?"
    rb"(?:\x07|\x1b\\|(?=\x1b)|(?=\n)|\Z))"
    rb"|(?P<alt>\x1b\[\?(?:1049|1047|47)h)"
    rb"|(?P<txt>[^\x1b\n]+|\x1b)",
    re.S,
)
# 代替画面の区間の終わりの候補（手前で止めるものは先読み）:
#   抜けた / RIS / シェル統合の印 / zsh の PROMPT_SP / readline の入力受付（直後がプロンプト）/
#   プロンプトの形をした行（ssh・nc の先で全画面アプリが閉じないまま接続が切れた・偽の ESC[?1049h）
_ALT_END = re.compile(
    rb"\x1b\[\?(?:1049|1047|47)l|\x1bc|(?=\x1b\](?:133;[ACD]|633;E))|(?=        \r)"
    rb"|(?=\x1b\[\?2004h)|(?<=\n)(?=[^\n])")
# 全画面アプリは必ずカーソルを絶対位置で動かす（CUP / HVP / スクロール領域）。これが無い区間は畳まない
_ABS_MOVE = re.compile(rb"\x1b\[[0-9;]*[Hfr]")
# プロンプトらしい行の頭（色指定を除いたもの）。cmd.exe / PowerShell / user@host$ / kali の 2 行など
_PROMPTISH = re.compile(
    rb"(?:\*Evil-WinRM\*\s+)?PS [^>\r\n]{1,260}> ?"
    rb"|[A-Za-z]:\\[^<>|\r\n]{0,260}>"
    rb"|(?:\([^)\r\n]{1,60}\)\s*)?[\w.-]{1,64}@[\w.-]{1,253}(?::[^\r\n$#]{0,260})?\s?[$#](?: |$)"
    rb"|\[[\w.-]{1,64}@[\w.-]{1,253}[^\]\r\n]{0,260}\][$#](?: |$)"
    rb"|\xe2\x94\x8c\xe2\x94\x80|\xe2\x95\xad\xe2\x94\x80|\xe2\x94\x94\xe2\x94\x80[$#]")
_CTL_SEQ = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\)?|\x1b[=>()][0-9A-Za-z]?")
_D_EXIT = re.compile(rb"D;(-?\d{1,10})")
# ;emaki=<合言葉>:<コマンド番号>（番号はシェルが実行するコマンドごとに 1 ずつ増やす）
_NONCE = re.compile(rb";emaki=([0-9a-f]{8,32})(?::(\d{1,9}))?\Z")
# 本物の印の末尾（終端記号まで）。トークン分割がこの印をまたいで飲み込まないよう、先に位置を拾う
_MARK_TAIL = re.compile(rb";emaki=([0-9a-f]{8,32})(?::\d{1,9})?(?=\x07|\x1b\\)")
_ESC_X = re.compile(rb"\\(?:x([0-9A-Fa-f]{2})|(\\))")
_SIG_TAIL = re.compile(rb"\r( {0,2})\r")
_MARK_BEFORE = re.compile(
    rb"(?:\x1b\[[0-9;]*m)*(?P<mk>[\x21-\x7e]|[\xc2-\xf4][\x80-\xbf]{1,3})(?:\x1b\[[0-9;]*m)*\Z")


class Line:
    """論理行。s/e は raw 上のバイトオフセット（e は行末の \\n または最後のバイト）。"""

    __slots__ = ("s", "e", "b", "marks", "alt", "rows", "ev", "text", "t0", "t1", "trunc")

    def __init__(self, s, e, b, marks):
        self.s = s
        self.e = e
        self.b = b
        self.marks = marks      # [(kind, value, offset, nonce, seq)] kind: A/C/D/E（A の value は cwd）
        self.alt = None         # 全画面アプリ区間ならそのバイト数
        self.rows = [[]]
        self.ev = {}
        self.text = ""
        self.t0 = None
        self.t1 = None
        self.trunc = 0


def _unesc(b):
    """シェル統合が送る \\xNN / \\\\ エスケープを戻す。"""
    return _ESC_X.sub(lambda m: bytes([int(m.group(1), 16)]) if m.group(1) else b"\\", b)


def _cwd_from_osc7(pl):
    # file://host/path。使うのは本物の A の直前のもの = 本プラグインのシェル統合が出したものだけで、
    # そちらは \xNN でしかエスケープしない（%XX はディレクトリ名の一部なので戻さない）
    if pl.startswith(b"file://"):
        rest = pl[7:]
        i = rest.find(b"/")
        path = rest[i:] if i >= 0 else b""
    else:
        path = pl
    return _unesc(path)


def _genuine_starts(raw, genuine):
    """本物の合言葉の付いた印（OSC 133 / 633）の開始位置の一覧（昇順）。"""
    out = []
    if not genuine:
        return out
    for m in _MARK_TAIL.finditer(raw):
        if m.group(1).decode() not in genuine:
            continue
        s = raw.rfind(b"\x1b]", 0, m.start())
        if s < 0 or raw[s + 2:s + 6] not in (b"133;", b"633;"):
            continue
        if raw.find(b"\x07", s, m.start()) >= 0 or raw.find(b"\x1b", s + 2, m.start()) >= 0 \
                or raw.find(b"\n", s, m.start()) >= 0:
            continue
        out.append(s)
    return out


def _sig_start(b, i, lo=0):
    """b[i:] が「空白 8 個 + CR」で始まる位置について、それが PROMPT_SP（空白の列 + CR +
    マーク幅の空白 + CR）なら (開始位置, 終了位置) を返す。開始位置はマークとその色指定を含む。"""
    cr = i + 8
    m = _SIG_TAIL.match(b, cr)
    if not m:
        return None
    w = len(m.group(1))
    j = i
    stop = max(cr - 1000, lo)
    while j > stop and b[j - 1] == 0x20:
        j -= 1
    if (cr - j) + w < 20:  # 幅 20 未満は PROMPT_SP とみなさない（プログレスバー等の空白 + CR と区別）
        return None
    if w:
        mm = _MARK_BEFORE.search(b, max(lo, j - 64), j)
        if not mm or char_width(mm.group("mk").decode("utf-8", "replace")[:1]) != w:
            return None
        j = mm.start()
    return j, m.end()


def _prompt_sp_cut(b):
    """論理行の途中にある zsh の PROMPT_SP の開始位置（無ければ -1）。
    改行で終わらなかった出力の直後に出るもの。後ろに入力行（ESC[?2004h）が続くか、zle の
    プロンプト描画（ESC[J で始まる。2 行プロンプトの上の行など）が続くか、そこで行が終わる
    （C/D で割られた行）場合だけを対象にする。"""
    if len(b) < 21:
        return -1
    lim = b.find(b"\x1b[?2004h")
    tail = len(b.rstrip(b" \r"))  # ここより後ろが空白と CR だけ = そこで行が終わる
    hit = -1
    i = b.find(b"        \r", 1)
    while i >= 0 and (lim < 0 or i < lim):
        sig = _sig_start(b, i)
        if sig is not None and sig[0] > 0 and (
                lim >= 0 or sig[1] >= tail
                or b.find(b"\x1b[J", sig[1], sig[1] + 64) >= 0):
            hit = sig[0]
        i = b.find(b"        \r", i + 9)
    return hit


def _promptish(raw, p):
    """raw[p:] の 1 行目（色などの制御列を除く）がプロンプトの形で始まるか。"""
    nl = raw.find(b"\n", p, p + 600)
    chunk = _CTL_SEQ.sub(b"", raw[p:nl if nl >= 0 else p + 600]).lstrip(b"\r")
    return _PROMPTISH.match(chunk) is not None


def _alt_end(raw, start, genuine):
    """代替画面の区間がどこで終わるか → (次に読む位置, アプリが自分で抜けたか)。見つからなければ末尾。"""
    n = len(raw)
    pos = start
    while True:
        m = _ALT_END.search(raw, pos)
        if not m:
            return n, True
        p = m.start()
        if m.end() > p:  # ESC[?1049l など / RIS: そこまでを区間に含める
            return m.end(), True
        c = raw[p]
        if c == 0x1b and raw[p + 1:p + 2] == b"]":  # シェル統合の印: 本物ならシェルに制御が戻っている
            om = _SPLIT.match(raw, p)
            nm = _NONCE.search(om.group("pl") or b"") if om.lastgroup == "osc" else None
            if nm and nm.group(1).decode() in genuine:
                return p, False
            pos = p + 2
            continue
        if c == 0x1b:  # ESC[?2004h: 直後がプロンプトなら readline（bash 等）が入力を受け付けている
            if _promptish(raw, p + 8):
                return p, False
            pos = p + 8
            continue
        if c == 0x20:
            sig = _sig_start(raw, p, start)
            if sig is not None:
                # PROMPT_SP の後ろ（同じ行の中）に入力受付があれば、シェルのプロンプトが戻っている
                nl = raw.find(b"\n", sig[1], sig[1] + 4096)
                q = raw.find(b"\x1b[?2004h", sig[1], nl if nl >= 0 else sig[1] + 4096)
                if q >= 0:
                    return sig[0], False
            pos = p + 9
            continue
        # 行頭: プロンプトの形の行（全画面アプリの描画は改行ではなく絶対位置への移動で行う）
        if _promptish(raw, p):
            return p, False
        pos = p + 1


def _alt_tail(raw, start, stop):
    """アプリが抜けないまま終わった区間（接続が切れた等）で、畳むのは最後の絶対位置への移動まで。
    そこから後ろ（ssh の「Connection ... closed」など、普通に流れた文字）は畳まずに見せる
    （その行の頭にはアプリの最後の描画の残りが付くことがある）。"""
    last = None
    for last in _ABS_MOVE.finditer(raw, start, stop):
        pass
    return last.end() if last is not None else stop


def split_raw(raw, nonces=None):
    """raw バイト列 → [Line]（まだレンダリングしない）。

    nonces: このペインのシェルが記録した本物の合言葉 {nonce: 記録時刻}。これに無い印は捨てる。
    """
    genuine = nonces or {}
    lines = []
    parts = []
    poffs = []   # parts の各要素の raw 上の開始位置
    marks = []
    start = 0
    pos = 0
    n = len(raw)
    match = _SPLIT.match
    last7 = (-1, None)  # 直前の OSC 7 の (終了位置, cwd)
    # 本物の印の手前でトークンを区切る（出力の最後のはぐれた ESC や書きかけの DCS などに、
    # 直後の本物の印が飲み込まれないように。端末も ESC が来ればそこから読み直す）
    gstarts = _genuine_starts(raw, genuine)
    gi = 0
    ng = len(gstarts)

    def flush(end):
        b = b"".join(parts)
        cut = _prompt_sp_cut(b) if b"        \r" in b else -1
        if cut > 0:
            # 改行で終わらなかった出力と、その後ろの PROMPT_SP（＋プロンプト）を別の行にする
            acc = 0
            off = end
            for p, o in zip(parts, poffs):
                if acc + len(p) > cut:
                    off = o + min(cut - acc, len(p))
                    break
                acc += len(p)
            lines.append(Line(start, max(off - 1, start), b[:cut], [x for x in marks if x[2] < off]))
            lines.append(Line(off, end, b[cut:], [x for x in marks if x[2] >= off]))
        else:
            lines.append(Line(start, end, b, marks[:]))
        del parts[:]
        del poffs[:]
        del marks[:]

    while pos < n:
        while gi < ng and gstarts[gi] <= pos:
            gi += 1
        m = match(raw, pos, gstarts[gi] if gi < ng else n)
        kind = m.lastgroup
        if kind == "txt":
            parts.append(m.group())
            poffs.append(pos)
            pos = m.end()
            continue
        if kind == "nl":
            flush(pos)
            start = pos + 1
            pos += 1
            continue
        if kind in ("dd", "dcs"):
            pos = m.end()
            continue
        if kind == "osc":
            num = m.group("num")
            pl = m.group("pl") or b""
            if num == b"7":
                last7 = (m.end(), _cwd_from_osc7(pl))
            elif num in (b"133", b"633"):
                nm = _NONCE.search(pl)
                nonce = nm.group(1).decode() if nm else None
                tok = m.group()
                if nonce is not None and nonce in genuine and tok[-1:] in (b"\x07", b"\\") \
                        and (nm.group(2) or pl[:1] == b"B"):
                    # 本物の合言葉・終端記号あり・コマンド番号あり（B 以外）のものだけを信用する
                    seq = int(nm.group(2)) if nm.group(2) else None
                    k = pl[:1]
                    if num == b"633":
                        if pl[:2] == b"E;":
                            marks.append(("E", _unesc(pl[2:nm.start()]), pos, nonce, seq))
                    elif k == b"B":
                        parts.append(b"\x1b]133;B\x07")  # プロンプト末尾の位置はレンダラで拾う
                        poffs.append(pos)
                    elif k in (b"C", b"D"):
                        if parts:  # 行の途中なら、ここで行を割る
                            flush(pos - 1)
                            start = pos
                        val = None
                        if k == b"D":
                            mm = _D_EXIT.match(pl)
                            if mm:
                                val = int(mm.group(1))
                        marks.append((k.decode(), val, pos, nonce, seq))
                    elif k == b"A":
                        cwd = last7[1] if last7[0] == pos else None
                        marks.append(("A", cwd, pos, nonce, seq))
            pos = m.end()
            continue
        if kind == "alt":
            stop, exited = _alt_end(raw, m.end(), genuine)
            if not _ABS_MOVE.search(raw, m.end(), min(stop, m.end() + 8192)):
                # カーソルを絶対位置へ動かさない = 全画面アプリではない（出力に混ざった ESC[?1049h 等）。
                # 畳まずに普通の出力として読む
                pos = m.end()
                continue
            if not exited:
                stop = _alt_tail(raw, m.end(), stop)
            if parts:
                flush(pos - 1)
            ln = Line(pos, stop - 1, b"", marks[:])
            ln.alt = stop - pos
            lines.append(ln)
            del marks[:]
            start = stop
            pos = stop
            continue
    if parts or marks:
        flush(n - 1)
    return lines


class TimeMap:
    """時刻 sidecar（"epoch 累積オフセット" の行）から、raw の任意バイトが書かれた時刻を引く。"""

    def __init__(self, recs):
        recs = sorted(recs, key=lambda x: (x[1], x[0]))
        self.off = [o for _, o in recs]
        self.t = [t for t, _ in recs]

    def __bool__(self):
        return bool(self.off)

    def time_of(self, x):
        # バイト x を書いたのは「累積が x を初めて超えたチャンク」= offset > x の最初のレコード
        i = bisect.bisect_right(self.off, x)
        if i >= len(self.off):
            i = len(self.off) - 1
        return self.t[i]

    @classmethod
    def load(cls, path):
        recs = []
        try:
            with open(path, "rb") as f:
                for ln in f:
                    m = re.match(rb"(\d{1,12})\s+(\d{1,15})", ln)
                    if m:
                        recs.append((int(m.group(1)), int(m.group(2))))
        except OSError:
            return None
        return cls(recs) if recs else None


_ESCSEQ = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-_][0-?]*)")


def _readline_width(s):
    """bash などの readline は、入力が行の右端に達すると「空白 + CR」を書いて端末を折り返させる。
    最初のその位置までの幅 = 入力したときの端末幅（あとからペインの幅を変えても正しく読める）。"""
    i = s.find(" \r", 1)
    if i < 0:
        return None
    seg = _ESCSEQ.sub("", s[:i]).lstrip("\r")
    if "\r" in seg or "\x08" in seg:
        return None
    w = text_width(seg)
    return w if w >= 20 else None


def prompt_sp_width(s):
    """行頭が zsh の PROMPT_SP ならそのときの端末幅、そうでなければ None。"""
    m = _PROMPT_SP.match(s)
    if not m:
        return None
    mk = m.group("mk")
    w = len(m.group("w"))
    if (char_width(mk) if mk else 0) != w:
        return None
    return len(m.group("sp")) + w


def render_lines(lines, tbl, encoding="utf-8", tmap=None, width=None):
    """各 Line をレンダリングし、rows / text / ev / 時刻を埋める。

    width: 端末幅が PROMPT_SP から分からない入力行に使う幅（索引に記録したペイン幅）。
    """
    prev_w = None
    bare = False  # 入力受付の印の無い PROMPT_SP の行（2 行プロンプトの上段・D の印で割れた行）の後、まだ入力行が無い
    age = 9
    for ln in lines:
        if tmap:
            ln.t0 = tmap.time_of(ln.s)
            ln.t1 = tmap.time_of(max(ln.e, ln.s))
        if ln.alt is not None:
            prev_w, bare = None, False
            continue
        s = ln.b.decode(encoding, errors="replace")
        ln.b = None  # メモリ節約（以降は使わない）
        own = prompt_sp_width(s)  # 行末の CR を落とす前に見る（PROMPT_SP だけの行は CR で終わる）
        eol = len(s)
        s = s.rstrip("\r")
        eol -= len(s)  # 改行の前の CR の数（zle は CR NL で次の行へ移るので 2、ふつうの出力の NL は 1）
        age += 1
        w = None
        zle = False
        if "\x1b[?2004h" in s:
            # 入力行だけは端末幅で折り返しを再現する（zle の消去処理が合うように）。2 行プロンプトでは
            # PROMPT_SP が上の行（シェル統合ありなら D の印で割れたさらに上の行）にあるので、
            # 2 行以内の PROMPT_SP の幅を使う。それも無ければペイン幅。行頭が ESC[?2004h の行
            # （bash の readline など。PROMPT_SP は出ない）は、出力に混ざった似た行の幅を使わない
            if s.lstrip("\r").startswith("\x1b[?2004h"):
                w = own or _readline_width(s) or width
            else:
                w = own or (prev_w if age <= 2 else None) or width
                # zsh（zle）の入力行: この行に PROMPT_SP があるか、印の無い PROMPT_SP の行の後の最初の入力行
                # （4 行以内で、印より前に readline の入力確定の印 ESC[?2004l が無い）。その後の入力行は、
                # 起動したプログラム（ssh 先・REPL など）や継続行のものなので含めない
                zle = bool(own) or (bare and age <= 4 and "\x1b[?2004l" not in s[:s.index("\x1b[?2004h")])
            bare = False
        if own:
            prev_w, age, bare = own, 0, "\x1b[?2004h" not in s
        rows, ev, trunc = render_line(s, tbl, w)
        if own:
            ev["psp"] = own   # PS1 の直前（zsh は PS2 の前には PROMPT_SP を出さない）
        if zle:
            ev["zsh"] = True  # zsh の入力行（印はプロンプトの後ろ。bash の readline のように前ではない）
        if eol:
            ev["eol"] = eol
        if w:
            ev["w"] = w
        ln.rows = rows
        ln.ev = ev
        ln.trunc = trunc
        ln.text = "\n".join("".join(t for t, _ in row) for row in rows)
    return lines


def lines_from_text(text, tbl, tsfmt_re=None):
    """整形済み .log（raw が残っていない場合の代替）から Line を作る。

    各行頭の日時プレフィックス（@rl_timestamp の出力）があれば時刻として拾い、取り除く。
    """
    out = []
    for i, raw_line in enumerate(text.split("\n")):
        t = None
        if tsfmt_re is not None:
            m = tsfmt_re[0].match(raw_line)
            if m:
                t = tsfmt_re[1](m.group(0))
                if t is not None:
                    raw_line = raw_line[m.end():]
        ln = Line(i, i, None, [])
        rows, ev, trunc = render_line(raw_line.rstrip("\r"), tbl)
        ln.rows = rows
        ln.ev = {}  # 整形済みテキストに入力開始の印は残っていない
        ln.text = "\n".join("".join(x for x, _ in row) for row in rows)
        ln.t0 = ln.t1 = t
        out.append(ln)
    while out and not out[-1].text:
        out.pop()
    return out
