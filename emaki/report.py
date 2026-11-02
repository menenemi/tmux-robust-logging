"""ブロック群をデータ（JSON）にして、HTML テンプレートに埋め込む。

出力は外部依存の無い 1 枚の HTML（CSS/JS/データすべて内蔵、オフラインで開ける）。
ログの中身は攻撃者が制御しうる文字列（pentest のログなら特に）なので、HTML 側では
textContent でしか描画しない。データ埋め込み時も "<" を \\u003c にして </script> を作らせない。

フォントは Fira Code。既定では名前で指定するだけ（入っている PC ではそれで表示される）。
embed_font=True なら、手元の Fira Code を data: URL の @font-face として埋め込む
（ネットワークからは何も読まない）。
"""

import base64
from bisect import bisect_left
import html
import json
import os
import re
import subprocess
import tempfile
import time

from . import __version__
from .render import char_width, text_width

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "template.html")
FONT_SLOT = "/*__RL_FONT__*/"


def _line_json(L):
    if L.alt is not None:
        return [{"alt": L.alt}]
    out = []
    for row in L.rows:
        if not row:
            out.append("")
        elif len(row) == 1 and row[0][1] == 0:
            out.append(row[0][0])
        else:
            out.append([[t, s] for t, s in row])
    if L.trunc:
        out.append({"cut": L.trunc})
    return out


def _stream(lines):
    """プロンプトと入力が書かれていた行の文字列と、1 文字ごとのスタイル番号。自動折り返しで続いた段は
    （input_split と同じく右端までの空白を補って）つなぎ、それ以外の段・行の間は改行にする。
    戻り値: (文字列, スタイル番号の列, 詰め物の位置)。詰め物 = 全角の文字が右端の 1 セルに入らなかったとき
    zle がそこに置いた空白（入力の文字列には無い）"""
    parts, sids, soft = [], [], []
    for L in lines:
        if L.alt is not None or not L.rows:
            continue
        if parts:
            parts.append("\n")
            sids.append(0)
        wraps = L.ev.get("wrap") or {}
        last = len(L.rows) - 1
        for x, row in enumerate(L.rows):
            for s, sid in row:
                parts.append(s)
                sids.extend([sid] * len(s))
            if x < last:
                if x in wraps:
                    t = "".join(s for s, _ in row)
                    pad = max(wraps[x] - text_width(t), 0)
                    parts.append(" " * pad)
                    sids.extend([0] * pad)
                    nx = "".join(s for s, _ in L.rows[x + 1])[:1]
                    if nx and char_width(nx) == 2 and (pad or t.endswith(" ")):
                        soft.append(len(sids) - 1)
                else:
                    parts.append("\n")
                    sids.append(0)
    return "".join(parts), sids, soft


_MISS = 16   # 続けてこれだけの行が画面に見つからなければ、残りの行は探さない（長い入力で時間をかけない）
_PART = 4    # 一部だけ合わせるときの最短の長さ（短いと関係の無い所にたまたま合う）


class _Screen:
    """色を拾う元の画面（プロンプトと入力が書かれていた行）。"""

    def __init__(self, rows):
        self.s, self.sids, self.soft = _stream(rows)
        w = max([L.ev.get("w") or 0 for L in rows if L.alt is None] + [0])
        # 探す範囲のゆとり（行の右の右プロンプト・継続行のプロンプト・行の間の改行の分）
        self.slack = max(256, w + 128)
        self._compact = None

    def compact(self):
        """詰め物を除いた文字列と、その各文字の元の位置。"""
        if self._compact is None:
            drop = set(self.soft)
            idx = [i for i in range(len(self.s)) if i not in drop]
            self._compact = ("".join(self.s[i] for i in idx), idx)
        return self._compact


def _find(scr, line, lo, hi, rev):
    """line を画面の [lo, hi) で探す（rev なら最後のもの）。全角の文字の前に詰め物があっても合わせる。
    戻り値: (開始位置, 終了位置, 1 文字ごとのスタイル番号) か None"""
    s = scr.s
    k = s.rfind(line, lo, hi) if rev else s.find(line, lo, hi)
    if k >= 0:
        return k, k + len(line), scr.sids[k:k + len(line)]
    if scr.soft and bisect_left(scr.soft, lo) < bisect_left(scr.soft, hi):
        c, idx = scr.compact()
        a, z = bisect_left(idx, lo), bisect_left(idx, hi)
        k = c.rfind(line, a, z) if rev else c.find(line, a, z)
        if k >= 0:
            pos = idx[k:k + len(line)]
            return pos[0], pos[-1] + 1, [scr.sids[p] for p in pos]
    return None


def _part(scr, line, lo, hi):
    """line 全体が見つからないとき（補完の一覧や Ctrl-R の検索の行の後で、入力の一部だけが別の行に
    描き直された等）、末尾と先頭のできるだけ長い部分を [lo, hi) の後ろから探して、その部分の色だけ拾う。
    戻り値は _find と同じ。"""
    s, n = scr.s, len(line)
    if n <= _PART:
        return None

    def longest(piece, a, z):
        # piece(m) が [a, z) にある最大の m と、その位置（長い部分があれば、その中の短い部分もある）
        best, m0, m1 = None, _PART, n - 1
        while m0 <= m1:
            m = (m0 + m1) // 2
            k = s.rfind(piece(m), a, z)
            if k >= 0:
                best, m0 = (m, k), m + 1
            else:
                m1 = m - 1
        return best

    tail = longest(lambda m: line[n - m:], lo, hi)
    head = longest(lambda m: line[:m], lo, tail[1] if tail else hi)
    if not (tail or head):
        return None
    out = [0] * n
    if head:
        out[:head[0]] = scr.sids[head[1]:head[1] + head[0]]
    if tail:  # 重なる所は後から描かれた方（末尾の部分）の色
        out[n - tail[0]:] = scr.sids[tail[1]:tail[1] + tail[0]]
    a = head[1] if head else tail[1]
    z = tail[1] + tail[0] if tail else head[1] + head[0]
    return a, z, out


def _colors_of(text, scr, lo, hi, rev=False):
    """text の各行を画面の [lo, hi) から探し、画面に出ていたときの色（スタイル番号）を拾う。
    rev: 後ろの行から、後ろにあるものを優先して探す（入力は、最後に描き直されたものの色が端末に残る）。
    各行は直前に見つかった行の近く（行の長さ + ゆとり）だけを探す。見つからない行は色なし。
    戻り値: (1 文字ごとのスタイル番号, 見つかった所の端: rev なら先頭、そうでなければ末尾)"""
    lines = text.split("\n")
    per = [None] * len(lines)
    miss = gap = 0
    for i in (range(len(lines) - 1, -1, -1) if rev else range(len(lines))):
        line = lines[i]
        per[i] = [0] * len(line)
        if not line:
            continue
        if miss >= _MISS:
            if not (rev and i == 0):
                continue
            a, z = lo, min(hi, lo + len(line) + scr.slack)  # 1 行目はプロンプトのすぐ後ろだけ探す
        elif rev:
            a, z = max(lo, hi - len(line) - scr.slack - gap), hi
        elif i == 0:
            a, z = lo, hi  # 最初の行は前に何があっても（改行で終わらなかった前の出力の後ろなど）探す
        else:
            a, z = lo, min(hi, lo + len(line) + scr.slack + gap)
        r = _find(scr, line, a, z, rev) or (_part(scr, line, a, z) if rev else None)
        if r is None:
            miss += 1
            gap += len(line) + 1
            continue
        k, e, per[i] = r
        if rev:
            hi = k
        else:
            lo = e
        miss = gap = 0
    out = []
    for i, p in enumerate(per):
        if i:
            out.append(0)
        out.extend(p)
    return out, (hi if rev else lo)


def _rle(text, per_char):
    """[長さ, スタイル番号] の列。長さは JavaScript の文字列の長さ（UTF-16 の単位）で数える。"""
    runs = []
    for ch, sid in zip(text, per_char):
        n = 2 if ord(ch) > 0xFFFF else 1
        if runs and runs[-1][1] == sid:
            runs[-1][0] += n
        else:
            runs.append([n, sid])
    return runs


def _prompt_colors(b, d):
    """プロンプト（pr）とコマンド（cr）の色を、それが書かれていた画面の行から付ける（端末と同じ色にする）。
    画面と文字列が合わない部分は色なし。どこにも色が無ければ付けない。"""
    rows = b.get("_rows")
    if not rows or not (b.get("cmd") or b.get("prompt")):
        return
    scr = _Screen(rows)
    end = len(scr.s)
    pos = 0
    if b.get("prompt"):
        per, pos = _colors_of(b["prompt"], scr, 0, end)
        if any(per):
            d["pr"] = _rle(b["prompt"], per)
    if b.get("cmd"):
        # コマンドはプロンプトより後ろの、最後に描き直されたもの（ジョブの通知・補完の一覧などの後の描き直し）
        per, _ = _colors_of(b["cmd"], scr, pos, end, rev=True)
        if any(per):
            d["cr"] = _rle(b["cmd"], per)


def block_json(b, pane, max_lines):
    out = []
    for L in b["lines"]:
        out.extend(_line_json(L))
    n = sum(1 for x in out if not isinstance(x, dict))
    if max_lines and max_lines > 0 and len(out) > max_lines:
        head = max_lines * 4 // 5
        tail = max_lines - head
        # 省略した「行」の数。全画面アプリ・長すぎる行のプレースホルダ（dict）は行ではないので数えない
        # （HTML 側は省略の後の行番号をこの数だけ進める）
        gap = sum(1 for x in out[head:len(out) - tail] if not isinstance(x, dict))
        out = out[:head] + [{"gap": gap}] + out[len(out) - tail:]
    d = {"kind": b["kind"], "pane": pane, "out": out, "n": n}
    for k in ("cmd", "prompt", "cwd", "remote", "label"):
        if b.get(k):
            d[k] = b[k]
    for k in ("t", "te", "exit"):
        if b.get(k) is not None:
            d[k] = b[k]
    for k in ("xi", "exact", "running", "screen"):
        if b.get(k):
            d[k] = True
    _prompt_colors(b, d)
    if b["children"]:
        d["children"] = [block_json(k, pane, max_lines) for k in b["children"]]
    return d


def write_html(data, out_path, title, embed_font=False):
    with open(TEMPLATE, encoding="utf-8") as f:
        tpl = f.read()
    js = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    js = js.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    head, tail = tpl.split("__RL_DATA__", 1)
    # フォントはデータより前（<style> の中）だけに入れる。タイトルより先に置き換えるので、
    # タイトルに同じ文字列が入っていても差し込み先にはならない
    head = head.replace(FONT_SLOT, font_css(find_fira_code()) if embed_font else "", 1)
    t = html.escape(title)
    page = head.replace("__RL_TITLE__", t) + js + tail.replace("__RL_TITLE__", t)
    blob = page.encode("utf-8", "replace")
    d = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(d, exist_ok=True)
    # 書きかけを読まれないよう一時ファイルに書いてから置き換える。ログは機微情報なので 600
    fd, tmp = tempfile.mkstemp(prefix=".emaki-", suffix=".html.tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return len(blob)  # バイト数


# ---- Fira Code -------------------------------------------------------------------------------

_FIRA_REG = re.compile(r"^firacode-regular\.(woff2|ttf|otf)$", re.I)
_FIRA_BOLD = re.compile(r"^firacode-bold\.(woff2|ttf|otf)$", re.I)
# 可変フォント（太さ 300〜700 を 1 ファイルで持つ）: FiraCode-VF / FiraCode[wght] / FiraCode-VariableFont_wght
_FIRA_VF = re.compile(r"^firacode(?:-vf|\[wght\]|-variable(?:font_wght)?)\.(woff2|ttf|otf)$", re.I)
_FONT_TYPES = {".woff2": ("font/woff2", "woff2"), ".ttf": ("font/ttf", "truetype"),
               ".otf": ("font/otf", "opentype")}
_EXT_RANK = {".woff2": 0, ".ttf": 1, ".otf": 2}   # 小さい順（woff2 が一番軽い）
_FONT_MAX = 4 * 1024 * 1024   # これより大きいファイルは埋め込まない（Fira Code は 1 MB 未満）
_WALK_MAX = 20000             # フォントディレクトリを見て回る件数の上限
_found = None                 # find_fira_code() の結果（プロセス内で 1 回だけ探す）


def _font_kind(name):
    if _FIRA_REG.match(name):
        return "reg"
    if _FIRA_BOLD.match(name):
        return "bold"
    if _FIRA_VF.match(name):
        return "vf"
    return None


def _usable(p):
    try:
        return os.path.isfile(p) and 0 < os.path.getsize(p) <= _FONT_MAX
    except OSError:
        return False


def _best(paths):
    return min(paths, key=lambda p: (_EXT_RANK.get(os.path.splitext(p)[1].lower(), 9), len(p), p))


def _font_dirs():
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return [os.path.join(HERE, "fonts"), os.path.join(xdg, "fonts"),
            os.path.join(os.path.expanduser("~"), ".fonts"), "/usr/local/share/fonts", "/usr/share/fonts"]


def _scan(root, budget):
    """root 以下を再帰で見て {種類: [パス]} を返す。budget は全体で共有する残り件数（[n]）。"""
    found = {}
    stack = [root]
    while stack and budget[0] > 0:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            continue
        with it:
            for e in it:
                budget[0] -= 1
                if budget[0] <= 0:
                    break
                try:
                    if e.is_dir(follow_symlinks=False):  # ディレクトリのリンクはたどらない（循環よけ）
                        stack.append(e.path)
                        continue
                    kind = _font_kind(e.name)
                    if kind and e.is_file():
                        found.setdefault(kind, []).append(e.path)
                except OSError:
                    continue
    return found


def _pick(found):
    regs = [p for p in found.get("reg", []) if _usable(p)]
    if regs:
        reg = _best(regs)
        bolds = [p for p in found.get("bold", []) if _usable(p)]
        same = [p for p in bolds if os.path.dirname(p) == os.path.dirname(reg)]
        return [reg, _best(same or bolds)] if bolds else [reg]
    vfs = [p for p in found.get("vf", []) if _usable(p)]
    return [_best(vfs)] if vfs else []


def _fc_match():
    """fontconfig に聞く（無い・遅い・失敗は全部 None）。"""
    try:
        r = subprocess.run(["fc-match", "-f", "%{file}", "Fira Code:style=Regular"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=2)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    p = r.stdout.decode("utf-8", "surrogateescape").strip()
    # fontconfig は無いフォントの代わりに別のフォントを返すので、名前が Fira Code のものだけ採る
    if not p or _font_kind(os.path.basename(p)) not in ("reg", "vf") or not _usable(p):
        return None
    return p


def _search():
    budget = [_WALK_MAX]
    for root in _font_dirs():
        if budget[0] <= 0:
            break
        if os.path.isdir(root):
            pick = _pick(_scan(root, budget))
            if pick:
                return pick
    p = _fc_match()
    if not p:
        return []
    if _font_kind(os.path.basename(p)) == "vf":
        return [p]
    bolds = []
    try:
        with os.scandir(os.path.dirname(p)) as it:
            for i, e in enumerate(it):
                if i >= 5000:
                    break
                if _FIRA_BOLD.match(e.name) and _usable(e.path):
                    bolds.append(e.path)
    except OSError:
        pass
    return [p, _best(bolds)] if bolds else [p]


def find_fira_code():
    """Fira Code のフォントファイルを探す。

    戻り値: [Regular（または可変フォント）, Bold（あれば）] か []。例外は投げない。
    探す場所: emaki/fonts/、$XDG_DATA_HOME/fonts（~/.local/share/fonts）、~/.fonts、
    /usr/local/share/fonts、/usr/share/fonts（再帰、件数上限あり）。無ければ fc-match。
    """
    global _found
    if _found is None:
        try:
            _found = _search()
        except Exception:
            _found = []
    return list(_found)


def font_css(files):
    """files（find_fira_code() の結果）を data: URL の @font-face にする。読めなければ ""。"""
    if not files:
        return ""
    reg = files[0]
    vf = _FIRA_VF.match(os.path.basename(reg)) is not None
    specs = [(reg, "300 700" if vf else "400")]
    if not vf and len(files) > 1 and files[1]:
        specs.append((files[1], "700"))
    faces = []
    for path, weight in specs:
        kind = _FONT_TYPES.get(os.path.splitext(path)[1].lower())
        data = None
        if kind:
            try:
                with open(path, "rb") as f:
                    data = f.read(_FONT_MAX + 1)
            except OSError:
                data = None
        if not data or len(data) > _FONT_MAX:
            if path == reg:
                return ""  # 標準の太さが無ければ埋め込まない（太字だけ別フォントになるのを避ける）
            continue
        faces.append('@font-face{font-family:"Fira Code";font-style:normal;font-weight:%s;'
                     'font-display:swap;src:url(data:%s;base64,%s) format("%s")}'
                     % (weight, kind[0], base64.b64encode(data).decode("ascii"), kind[1]))
    return "\n".join(faces)


def meta(sess, extra):
    lt = time.localtime()
    m = {
        "session": sess["name"],
        "sessionId": sess["id"],
        "host": sess.get("host", ""),
        "generated": int(time.time()),
        "tz": lt.tm_gmtoff if hasattr(lt, "tm_gmtoff") else -time.timezone,
        "tzName": time.strftime("%Z", lt),
        "version": __version__,
    }
    m.update(extra)
    return m
