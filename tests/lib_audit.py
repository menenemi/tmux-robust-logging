"""Shared harness for the colon-time audit (read-only use of the plugin).

Byte model: zsh 5.9
(PROMPT_SP, precmd marks D/OSC7/A, zle start, PROMPT, ESC[K ESC[?1h ESC= ESC[?2004h, typed, accept
ESC[?1l ESC> ESC[?2004l CR CR LF, preexec E/C), remote bash 5.1 readline (ESC[?2004h BEFORE PS1, typed,
CR LF, ESC[?2004l CR), RPROMPT drawn by zle as in final_realistic/sess.py (ESC[K, CUF, rprompt, CR, CUF).

The user's prompt in two time forms:
  colon   (current, Oct 2026)  ... ESC[1m ESC[38;2;137;184;194m 21:39:52 ESC[0m ESC[38;2;137;184;194m ESC[39m ...
  compact (August 2026)        ... ESC[1m ESC[38;2;137;184;194m 213952   ESC[0m ESC[38;2;137;184;194m 04 ESC[39m ...
Both are 8 cells wide, so every column-based decision sees identical geometry; only the text differs.
"""
import json
import os
import re
import sys

PLUGIN = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, PLUGIN)
from emaki.parse import split_raw, render_lines, lines_from_text  # noqa: E402
from emaki.render import StyleTable, text_width, render_line  # noqa: E402
from emaki.report import block_json  # noqa: E402
from emaki.segment import build_blocks  # noqa: E402
from emaki.timefmt import clean_log_parser  # noqa: E402

N1 = "0123456789abcdef"   # local zsh (genuine)
N2 = "fedcba9876543210"   # nested zsh started from N1 (genuine, parent N1)
NR = "a1b2c3d4e5f60718"   # remote shell over ssh (its registry is remote -> NOT genuine here)
REG = {N1: {"order": 0}, N2: {"order": 1, "parent": N1}}
COLS = 100

TASK_COLON = (b"\x1b[38;2;160;147;199m2026-10-29\x1b[39m \x1b[38;2;137;184;194m\x1b[1m\x1b[38;2;137;184;194m21:39:52"
              b"\x1b[0m\x1b[38;2;137;184;194m\x1b[39m \x1b[38;2;132;160;198m\x1b[1m\x1b[38;2;132;160;198mhost\x1b[0m"
              b"\x1b[38;2;132;160;198m\x1b[39m \x1b[38;2;198;200;209m\x1b[1m\x1b[38;2;198;200;209m~\x1b[0m"
              b"\x1b[38;2;198;200;209m\x1b[39m \x1b[38;2;180;190;130m\x1b[1m\x1b[38;2;180;190;130m\xe2\x9d\xaf\x1b[0m"
              b"\x1b[38;2;180;190;130m\x1b[39m ")
TASK_COMPACT = TASK_COLON.replace(b"21:39:52\x1b[0m\x1b[38;2;137;184;194m",
                                  b"213952\x1b[0m\x1b[38;2;137;184;194m04")


def seg_date(date):
    return b"\x1b[38;2;160;147;199m" + date + b"\x1b[39m"


def seg_time(main, frac):
    return b"\x1b[38;2;137;184;194m\x1b[1m\x1b[38;2;137;184;194m" + main + b"\x1b[0m\x1b[38;2;137;184;194m" + frac + \
        b"\x1b[39m"


def seg_host(host):
    return b"\x1b[38;2;132;160;198m\x1b[1m\x1b[38;2;132;160;198m" + host + b"\x1b[0m\x1b[38;2;132;160;198m\x1b[39m"


def seg_cwd(cwd):
    return b"\x1b[38;2;198;200;209m\x1b[1m\x1b[38;2;198;200;209m" + cwd + b"\x1b[0m\x1b[38;2;198;200;209m\x1b[39m"


SEG_ARROW = b"\x1b[38;2;180;190;130m\x1b[1m\x1b[38;2;180;190;130m\xe2\x9d\xaf\x1b[0m\x1b[38;2;180;190;130m\x1b[39m "
FRAME_TOP = b"\x1b[38;5;244m\xe2\x95\xad\xe2\x94\x80\x1b[39m "        # "╭─ "
FRAME_BOT = b"\x1b[38;5;244m\xe2\x95\xb0\xe2\x94\x80\x1b[39m"         # "╰─"


def time_text(form, hms, frac):
    """the visible time text (8 cells in both forms)"""
    if form == "colon":
        return hms[0:2] + b":" + hms[2:4] + b":" + hms[4:6]
    return hms + frac


def prompt_bytes(form, hms=b"213952", frac=b"04", date=b"2026-10-29", host=b"host", cwd=b"~", two_line=None):
    if form == "colon":
        tm = seg_time(time_text(form, hms, frac), b"")
    else:
        tm = seg_time(hms, frac)
    top = seg_date(date) + b" " + tm + b" " + seg_host(host) + b" " + seg_cwd(cwd)
    if two_line == "lean":       # p10k 2-line, no frame: date/time at the very start of the top row
        return top + b"\r\n" + SEG_ARROW
    if two_line == "frame":      # p10k 2-line with frame
        return FRAME_TOP + top + b"\r\n" + FRAME_BOT + SEG_ARROW
    return top + b" " + SEG_ARROW


assert prompt_bytes("colon") == TASK_COLON, "colon builder must reproduce the task's exact bytes"
assert prompt_bytes("compact") == TASK_COMPACT


def osc(x):
    return b"\x1b]" + x + b"\x07"


def tag(n, k):
    return b";emaki=" + n.encode() + b":%d" % k


def esc(cmd):
    out = b""
    for ch in cmd:
        c = bytes([ch])
        if c == b"\\":
            out += b"\\\\"
        elif c == b";":
            out += b"\\x3b"
        elif ch < 0x20 or ch == 0x7f:
            out += b"\\x%02x" % ch
        else:
            out += c
    return out


def precmd(n, k, ec=None, cwd=b"/home/user", host=b"host"):
    d = osc(b"133;D;%d" % ec + tag(n, k)) if ec is not None else b""
    return d + osc(b"7;file://" + host + esc(cwd)) + osc(b"133;A" + tag(n, k))


def preexec(n, k, cmd):
    return osc(b"633;E;" + esc(cmd) + tag(n, k)) + osc(b"133;C" + tag(n, k))


def PSP(cols=COLS):
    return b" " * cols + b"\r\r"


ZSTART = b"\r\x1b[0m\x1b[27m\x1b[24m\x1b[J"
ZIN = b"\x1b[K\x1b[?1h\x1b=\x1b[?2004h"
ZACC = b"\x1b[?1l\x1b>\x1b[?2004l\r\r\n"
ZINTR = ZACC
BP_ON = b"\x1b[?2004h"
RL_OFF81 = b"\x1b[?2004l\r"


def G(x):  # zsh-syntax-highlighting command colour
    return b"\x1b[36m" + x + b"\x1b[39m"


class Clock:
    """Each drawn prompt gets its own time; records every time handed out (for normalization)."""

    def __init__(self, start=(22, 10, 3), frac=4, log=None):
        # starts at 22:10:03 so no drawn prompt time coincides with a time printed by a scenario's commands
        # (those use 21:3x-21:5x, 12:xx, 19:xx); the task's exact 21:39:52 bytes are asserted separately above
        self.h, self.m, self.s = start
        self.f = frac
        self.log = log if log is not None else []

    def next(self):
        hms = b"%02d%02d%02d" % (self.h, self.m, self.s)
        fr = b"%02d" % self.f
        self.log.append((hms, fr))
        self.s += 13
        self.f = (self.f * 7 + 11) % 100
        while self.s >= 60:
            self.s -= 60
            self.m += 1
        return hms, fr


class Z:
    """zsh 5.9 + the user's p10k prompt (+ emaki-integration.zsh when nonce is given)."""

    def __init__(self, form, nonce=N1, cols=COLS, clock=None, host=b"host", cwd=b"~", seq=0, two_line=None,
                 rprompt=None, oscpath=b"/home/user"):
        self.form = form
        self.n = nonce
        self.cols = cols
        self.clock = clock or Clock()
        self.host, self.cwd = host, cwd
        self.oscpath = oscpath
        self.seq = seq
        self.running = False
        self.ec = 0
        self.last_ec = 0
        self.two_line = two_line
        self.rprompt = rprompt
        self.s = b""
        self.last_left = b""

    def left(self):
        hms, fr = self.clock.next()
        b = prompt_bytes(self.form, hms, fr, host=self.host, cwd=self.cwd, two_line=self.two_line)
        self.last_left = b
        return b

    def left_w(self):
        """display width of the last prompt row INCLUDING its trailing space (zle puts the cursor after it;
        measuring a rendered row would drop the trailing blank and misplace the cursor by one)"""
        last = self.last_left.split(b"\r\n")[-1]
        return text_width(re.sub(r"\x1b\[[0-9;]*m", "", last.decode()))

    def prompt(self):
        s = PSP(self.cols)
        if self.n:
            s += precmd(self.n, self.seq, self.ec if self.running else None, cwd=self.oscpath, host=b"host")
        self.running = False
        s += ZSTART + self.left()
        if self.rprompt:
            rp, rpw = self.rprompt(self.last_ec)
            if rp:
                pw = self.left_w()
                if self.two_line:
                    # p10k 2-line: right prompt on the FIRST row; zle draws it when it draws the prompt (modelled
                    # as: up one row, CUF to the right edge, rprompt, back down to the end of the left prompt)
                    s += b"\x1b[K\x1b[A\r\x1b[%dC" % (self.cols - 1 - rpw) + rp + b"\r\x1b[B\r\x1b[%dC" % pw
                    s += b"\x1b[?1h\x1b=\x1b[?2004h"
                else:
                    s += b"\x1b[K" + b"\x1b[%dC" % (self.cols - 1 - rpw - pw) + rp + b"\r\x1b[%dC" % pw + \
                        b"\x1b[?1h\x1b=\x1b[?2004h"
            else:
                s += ZIN
        else:
            s += ZIN
        self.s += s
        return self

    def type(self, b):
        self.s += b
        return self

    def accept(self, cmd, typed=None, ps2=()):
        s = (cmd.split(b"\n")[0] if typed is None else typed) + ZACC
        for p2, ln in ps2:
            s += p2 + ZIN + ln + ZACC
        if self.n:
            self.seq += 1
            s += preexec(self.n, self.seq, cmd)
        self.running = True
        self.s += s
        return self

    def ctrl_c(self, typed=b""):
        self.s += typed + ZINTR
        self.last_ec = 130
        return self

    def empty_enter(self):
        self.s += ZACC
        return self

    def out(self, data, ec=0):
        self.s += data
        self.ec = ec
        self.last_ec = ec
        return self

    def raw(self, data):
        self.s += data
        return self

    def notice(self, text, typed_again=b""):
        self.s += b"\r\r\n" + text + b"\r\n" + ZSTART + self.last_left + b"\x1b[K" + typed_again
        return self


def rl(prompt, cmd, off=RL_OFF81):
    """bash 5.1 readline line: bracketed paste on BEFORE PS1, typed, CR LF, bracketed paste off + CR"""
    return BP_ON + prompt + cmd + b"\r\n" + off


# ------------------------------------------------------------------ pipelines (as cli.py / mk_r2_pages.py)
def run_raw(raw, reg=REG, width=COLS):
    tbl = StyleTable()
    lines = render_lines(split_raw(raw, reg), tbl, "utf-8", None, width=width)
    bl, mode = build_blocks(lines, None, reg)
    js = [block_json(b, 0, 20000) for b in bl]
    return js, mode, tbl, bl


def run_capture(data, width=COLS):
    """cli.py:175-186 (pane without a log: tmux capture-pane -p -e -J)."""
    tbl = StyleTable()
    lines = render_lines(split_raw(data), tbl, width=width)
    bl, mode = build_blocks(lines, None, None)
    js = [block_json(b, 0, 20000) for b in bl]
    return js, mode, tbl, bl


def run_clean(text, tsfmt=None, start=None):
    """cli.py:73-78 (raw gone: formatted .log)."""
    tbl = StyleTable()
    lines = lines_from_text(text, tbl, clean_log_parser(text, tsfmt, start))
    bl, mode = build_blocks(lines, None, None)
    js = [block_json(b, 0, 20000) for b in bl]
    return js, mode, tbl, bl


def screen_rows(raw, width=COLS):
    """What the screen showed, row by row (plain text): used to build capture-pane / formatted-log inputs."""
    lines = render_lines(split_raw(raw, REG), StyleTable(), "utf-8", None, width=width)
    rows = []
    for L in lines:
        if L.alt is not None:
            continue
        for r in L.rows:
            rows.append("".join(t for t, _ in r))
    return rows


# ------------------------------------------------------------------ normalization (colon vs compact)
TOK = "TTTTTTTT"


def time_strings(form, log):
    """time texts to mask.  Both representations are masked (a log may switch form mid-session, ADV19); neither
    can occur in scenario output (outputs never print 22:1x times or 2210xxxx digit runs)."""
    return {time_text(f, h, fr).decode() for h, fr in log for f in ("colon", "compact")}


def _style(tbl, sid):
    fg, bg, fl = tbl.list[sid]
    return [fg, bg, fl]


def _per_char_runs(text, runs, tbl):
    """[[len, sid], ...] (UTF-16 lengths) -> per-char style list"""
    out = []
    i = 0
    chars = list(text)
    for n, sid in runs:
        k = 0
        while k < n and i < len(chars):
            st = _style(tbl, sid)
            out.append(st)
            k += 2 if ord(chars[i]) > 0xFFFF else 1
            i += 1
    while i < len(chars):
        out.append(["", "", 0])
        i += 1
    return out


def _mask(text, times):
    """replace time texts by TOK; return (new text, set of masked positions)"""
    pos = set()
    if not text:
        return text, pos
    # a narrow pane can soft-wrap the prompt inside the time token: "21:3\n9:52" / "2139\n5204"
    variants = []
    for t in times:
        variants.append((t, TOK))
        for k in range(1, len(t)):
            variants.append((t[:k] + "\n" + t[k:], TOK[:k] + "\n" + TOK[k:]))
    for t, rep in variants:
        start = 0
        while True:
            k = text.find(t, start)
            if k < 0:
                break
            pos.update(x for x in range(k, k + len(t)) if text[x] != "\n")
            text = text[:k] + rep + text[k + len(t):]
            start = k + len(rep)
    return text, pos


def _norm_styles(styles, pos):
    return [[s[0], s[1], (None if i in pos else s[2])] for i, s in enumerate(styles)]


def normalize(js, tbl, times):
    """block JSON -> comparable structure: time text masked, styles resolved, bold flag ignored inside time.
    Output rows are masked as one text joined by newlines (a narrow pane splits a prompt row, and the time in it,
    over several output rows)."""
    def rows(out):
        if not out:
            return []
        texts, styles = [], []
        for r in out:
            if isinstance(r, str):
                texts.append(r)
                styles.append(None)
            elif isinstance(r, list):
                texts.append("".join(x[0] for x in r))
                st = []
                for s, sid in r:
                    st.extend([_style(tbl, sid)] * len(s))
                styles.append(st)
            else:
                texts.append("")
                styles.append(None)
        joined = "\n".join(texts)
        masked, pos = _mask(joined, times)
        parts = masked.split("\n")
        assert len(parts) == len(texts)
        res = []
        off = 0
        for i, r in enumerate(out):
            n = len(texts[i])
            if not isinstance(r, (str, list)):
                res.append({"special": r})
            elif styles[i] is None:
                res.append({"t": parts[i]})
            else:
                rp = {p - off for p in pos if off <= p < off + n}
                res.append({"t": parts[i], "s": _norm_styles(styles[i], rp)})
            off += n + 1
        return res

    def blk(d):
        e = {}
        for k in ("kind", "cmd", "prompt", "cwd", "remote", "label", "exit", "xi", "exact", "running", "screen", "n"):
            if k in d:
                e[k] = _mask(d[k], times)[0] if isinstance(d[k], str) else d[k]
        for k, src in (("pr", "prompt"), ("cr", "cmd")):
            if k in d:
                txt = d[src]
                st = _per_char_runs(txt, d[k], tbl)
                _, pos = _mask(txt, times)
                e[k] = _norm_styles(st, pos)
        e["out"] = rows(d.get("out", []))
        if d.get("children"):
            e["children"] = [blk(c) for c in d["children"]]
        return e

    return [blk(d) for d in js]


def first_diff(a, b, path="blocks"):
    """first differing path between two normalized structures (or None)"""
    if type(a) is not type(b):
        return path, a, b
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return path + "." + k, a.get(k, "<missing>"), b.get(k, "<missing>")
            r = first_diff(a[k], b[k], path + "." + k)
            if r:
                return r
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return path + ".len", len(a), len(b)
        for i, (x, y) in enumerate(zip(a, b)):
            r = first_diff(x, y, "%s[%d]" % (path, i))
            if r:
                return r
        return None
    return None if a == b else (path, a, b)


# ------------------------------------------------------------------ summaries / expectations
def row_text(r):
    if isinstance(r, str):
        return r
    if isinstance(r, list):
        return "".join(x[0] for x in r)
    return r


def brief(d, ind=""):
    s = "%s[%s] cmd=%r exit=%r%s prompt=%r%s%s" % (
        ind, d["kind"], d.get("cmd"), d.get("exit"), "(xi)" if d.get("xi") else "", d.get("prompt"),
        " exact" if d.get("exact") else "", (" remote=%r" % d["remote"]) if d.get("remote") else "")
    lines = [s]
    rows = [row_text(r) for r in d.get("out", [])]
    for t in rows[:10]:
        lines.append("%s    | %r" % (ind, t))
    if len(rows) > 10:
        lines.append("%s    | ... %d rows" % (ind, len(rows)))
    for c in d.get("children", []):
        lines.append(brief(c, ind + "      "))
    return "\n".join(lines)


def shape(js, times):
    """compact structural summary used for expectations: [(kind, cmd, exit, [child cmds]), ...]"""
    out = []
    for d in js:
        out.append((d["kind"], _mask(d.get("cmd") or "", times)[0], d.get("exit"),
                    [(c.get("remote"), c.get("cmd")) for c in d.get("children", [])]))
    return out


def out_texts(d):
    return [row_text(r) for r in d.get("out", []) if not isinstance(r, dict)]


def dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
