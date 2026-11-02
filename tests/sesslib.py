"""Byte builders for long, realistic sessions (on top of lib_audit's zsh 5.9 + p10k model).

Only the byte model of lib_audit is used here (Z, rl, precmd marks ...).  screen_rows() renders with whatever emaki is on
sys.path first, but rows/text do not depend on the change under review (render_line is identical in BASE and FIXED).
"""
import base64
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib_audit as L  # noqa: E402
from lib_audit import Z, N1, N2, NR, rl, BP_ON, ZIN, ZACC, ZSTART  # noqa: E402,F401

SAN = os.path.join(HERE, "..", "scripts", "sanitize.pl")
RL_OFF82 = b"\x1b[?2004l"           # readline 8.2 (bash 5.2, python3 on Kali)
RL_OFF81 = b"\x1b[?2004l\r"         # readline 8.1


def Lines(*rows):
    return b"".join((r.encode() if isinstance(r, str) else r) + b"\r\n" for r in rows)


class RP:
    """p10k right prompt: status (✘ N / ✘ INT) and command_execution_time (took Ns) of the previous command."""

    def __init__(self, took=True, jobs=False):
        self.use_took = took
        self.took = None      # set by the session before the next prompt
        self.jobs = 0
        self.use_jobs = jobs

    def __call__(self, ec):
        parts, w = [], 0
        if ec:
            s = b"INT" if ec == 130 else b"%d" % ec
            parts.append(b"\x1b[31m\xe2\x9c\x98 " + s + b"\x1b[39m")
            w += 2 + len(s)
        if self.use_took and self.took:
            t = b"took " + self.took
            parts.append(b"\x1b[33m" + t + b"\x1b[39m")
            w += len(t)
        if self.use_jobs and self.jobs:
            parts.append(b"\x1b[32m\xe2\x9c\xa6\x1b[39m")
            w += 1
        if not parts:
            return b"", 0
        w += len(parts) - 1
        return b" ".join(parts), w


def _plain(b):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]", "", b.decode("utf-8"))


def cursor_after(text, cols):
    """terminal (DECAWM + xenl, as tmux) position after printing text from column 0: (row, col); col == cols means
    the cursor waits at the right edge (pending wrap).  A wide char that does not fit wraps whole."""
    from emaki.render import char_width
    row, col = 0, 0
    for ch in text:
        w = char_width(ch)
        if w == 0:
            continue
        if col + w > cols:
            row += 1
            col = 0
        col += w
    return row, col


def back_to(cl, vcs):
    """zle_refresh.c moveto() on the same row after drawing the right prompt (cursor at winw - 1): CR + CUF when the
    target column is left of vcs/2, otherwise CUB"""
    if cl == vcs:
        return b""
    if cl < vcs / 2:
        return b"\r" + (b"\x1b[%dC" % cl if cl > 0 else b"")
    return b"\x1b[%dD" % (vcs - cl)


class ZR(Z):
    """lib_audit.Z, but zle's right prompt follows zle_refresh.c also when the left prompt is wider than the pane:
    the right prompt goes on the row where the left prompt ENDS (put_rpmpt: lpromptw + rpromptw < winw - 1), the
    cursor returns by moveto() (CR+CUF or CUB); a last prompt row that exactly fills the pane is followed by "\\n";
    on accept, zle removes the right prompt first if the typed text reached it."""

    lastcol = None
    pre = None     # callable -> bytes printed by a precmd FUNCTION (runs before precmd_functions = before D/A)

    def prompt(self):
        from lib_audit import PSP, precmd
        s = PSP(self.cols)
        if self.pre is not None:
            s += self.pre()
        if self.n:
            s += precmd(self.n, self.seq, self.ec if self.running else None, cwd=self.oscpath, host=b"host")
        self.running = False
        s += ZSTART + self.left()
        if self.two_line:
            # keep lib_audit's 2-line model (right prompt on the first row)
            self.s += s
            tail = b""
            if self.rprompt:
                rp, rpw = self.rprompt(self.last_ec)
                if rp:
                    pw = self.left_w()
                    tail = (b"\x1b[K\x1b[A\r\x1b[%dC" % (self.cols - 1 - rpw) + rp + b"\r\x1b[B\r\x1b[%dC" % pw
                            + b"\x1b[?1h\x1b=\x1b[?2004h")
            self.s += tail or ZIN
            self.lastcol = None
            return self
        last = _plain(self.last_left.split(b"\r\n")[-1])
        _, col = cursor_after(last, self.cols)
        if col >= self.cols:
            s += b"\r\n"
            col = 0
        self.lastcol = col
        rp, rpw = (self.rprompt(self.last_ec) if self.rprompt else (b"", 0))
        if rp and col + rpw < self.cols - 1:
            gap = self.cols - 1 - rpw - col
            s += b"\x1b[K" + (b"\x1b[%dC" % gap if gap > 0 else b"") + rp + back_to(col, self.cols - 1) + \
                b"\x1b[?1h\x1b=\x1b[?2004h"
        else:
            s += ZIN
        self.s += s
        return self

    def accept(self, cmd, typed=None, ps2=()):
        from emaki.render import text_width
        t = cmd.split(b"\n")[0] if typed is None else typed
        rp, rpw = (self.rprompt(self.last_ec) if self.rprompt else (b"", 0))
        if rp and self.lastcol is not None and self.lastcol + rpw < self.cols - 1 and \
                self.lastcol + text_width(t.decode("utf-8", "replace")) + rpw >= self.cols - 1:
            self.s += b"\x1b[K"
        return Z.accept(self, cmd, typed=typed, ps2=ps2)


class Sess:
    """zsh pane session.  Wraps ZR with cwd changes, slow commands and the right prompt."""

    def __init__(self, nonce=N1, cols=100, rprompt=True, two_line=None, took=True, jobs=False):
        self.rp = RP(took=took, jobs=jobs) if rprompt else None
        self.z = ZR("colon", nonce=nonce, cols=cols, two_line=two_line, rprompt=self.rp)

    @property
    def s(self):
        return self.z.s

    def cd(self, disp, path):
        self.z.cwd = disp
        self.z.oscpath = path

    def run(self, cmd, out=b"", ec=0, took=None, typed=None, cd=None):
        z = self.z
        z.prompt()
        z.accept(cmd, typed=typed)
        z.out(out, ec=ec)
        if cd:
            self.cd(*cd)
        if self.rp is not None:
            self.rp.took = took
        return self

    def prompt(self):
        self.z.prompt()
        return self

    def empty(self):
        self.z.prompt().empty_enter()
        if self.rp is not None:
            self.rp.took = None
        return self

    def ctrl_c(self, typed):
        self.z.prompt().ctrl_c(typed)
        if self.rp is not None:
            self.rp.took = None
        return self


# ---------------------------------------------------------------- realistic outputs
GIT_STATUS = Lines("On branch main", "Your branch is up to date with 'origin/main'.", "",
                   "Changes not staged for commit:",
                   '  (use "git add <file>..." to update what will be committed)',
                   '  (use "git restore <file>..." to discard changes in working directory)',
                   "\x1b[31m\tmodified:   app/main.py\x1b[m", "",
                   'no changes added to commit (use "git add" and/or "git commit -a")')
K = b"\x1b[K"
GIT_PULL = (b"remote: Enumerating objects: 9, done." + K + b"\r\n"
            b"remote: Counting objects:  11% (1/9)" + K + b"\rremote: Counting objects: 100% (9/9), done." + K +
            b"\r\nremote: Total 5 (delta 2), reused 5 (delta 2), pack-reused 0 (from 0)" + K + b"\r\n"
            b"Unpacking objects:  20% (1/5)\rUnpacking objects: 100% (5/5), 1.02 KiB | 1.02 MiB/s, done.\r\n"
            + Lines("From git.example.com:team/notes-api", "   8d41c2e..b7e09f3  main       -> origin/main",
                    "Updating 8d41c2e..b7e09f3", "Fast-forward",
                    " tests/test_notes.py | 9 +++++++++", " 1 file changed, 9 insertions(+)"))
PYTEST_FAIL = Lines(".venv/bin/pytest -q",
                    "..F.........                                                             [100%]",
                    "=================================== FAILURES ===================================",
                    "________________________ test_create_note_empty_title _________________________",
                    "E       assert 500 == 201", "tests/test_notes.py:42: AssertionError",
                    "=========================== short test summary info ============================",
                    "FAILED tests/test_notes.py::test_create_note_empty_title - assert 500 == 201",
                    "1 failed, 11 passed in 0.84s", "make: *** [Makefile:9: test] Error 1")
PYTEST_OK = Lines(".venv/bin/pytest -q",
                  "............                                                             [100%]",
                  "12 passed in 11.79s")
VIM = (b"\x1b[?1049h\x1b[22;0;0t\x1b[>4;2m\x1b[?1h\x1b=\x1b[H\x1b[2J\x1b[1;1Hfrom fastapi import FastAPI"
       b"\x1b[2;1H\x1b[94m~\x1b[0m\x1b[40;1H\"app/main.py\" 11L, 312B"
       b"\x1b[?1l\x1b>\x1b[>4;m\x1b[?1049l\x1b[23;0;0t")
GIT_LOG = Lines("\x1b[33m3f2c1ab\x1b[m Fix 500 on POST /notes when the title is empty",
                "\x1b[33mb7e09f3\x1b[m Add a test for empty note titles",
                "\x1b[33m8d41c2e\x1b[m Add pagination to GET /notes")
MOTD = Lines("Welcome to Ubuntu 24.04.1 LTS (GNU/Linux 6.8.0-45-generic x86_64)", "",
             " * Documentation:  https://help.ubuntu.com", "",
             "Last login: Wed Oct 28 18:02:41 2026 from 10.0.1.5")
PS1_WEB = b"\x1b[01;32mdeploy@web01\x1b[00m:\x1b[01;34m~\x1b[00m$ "
PS1_KALI = b"\x1b[01;32muser@host\x1b[00m:\x1b[01;34m~/projects/notes-api\x1b[00m$ "
KB_TOP = (b"\x1b[;32m\xe2\x94\x8c\xe2\x94\x80\xe2\x94\x80(\x1b[1;34muser\xe3\x89\xbfhost\x1b[;32m)-[\x1b[0;1m~/projects"
          b"\x1b[;32m]")
KB_BOT = b"\x1b[;32m\xe2\x94\x94\xe2\x94\x80\x1b[1;34m$\x1b[0m "
PS1_KB = KB_TOP + b"\r\n" + KB_BOT


def zsh_remote_prompt(host=b"devbox", cols=100, eol=True):
    """a remote zsh with the stock prompt (%m%# ) and the stock PROMPT_EOL_MARK (bold inverse %)"""
    if eol:
        psp = b"\x1b[1m\x1b[7m%\x1b[27m\x1b[1m\x1b[0m" + b" " * (cols - 1) + b"\r \r"
    else:
        psp = b" " * cols + b"\r\r"
    return psp + ZSTART + host + b"% " + ZIN


def remote_zsh(cmds, host=b"devbox", cols=100):
    """[(typed, output)] at a remote stock zsh"""
    s = b""
    for typed, out in cmds:
        s += zsh_remote_prompt(host, cols) + typed + ZACC + out
    return s


def bash_lines(ps1, cmds, off=RL_OFF82):
    """[(typed, output)] at a readline prompt (mark BEFORE the prompt, accept after the newline)"""
    s = b""
    for typed, out in cmds:
        s += BP_ON + ps1 + typed + b"\r\n" + off + out
    return s


# ---------------------------------------------------------------- forms
def screen_rows_joined(raw, width):
    """capture-pane -p -J: what the screen showed, one row per logical line, auto-wrapped rows joined"""
    from emaki.parse import split_raw, render_lines
    from emaki.render import StyleTable
    lines = render_lines(split_raw(raw, L.REG), StyleTable(), "utf-8", None, width=width)
    rows = []
    for ln in lines:
        if ln.alt is not None:
            continue
        texts = ["".join(t for t, _ in r) for r in ln.rows]
        wraps = ln.ev.get("wrap") or {}
        cur = None
        for i, t in enumerate(texts):
            if cur is None:
                cur = t
            else:
                cur += t
            if i in wraps and i + 1 < len(texts):
                from emaki.render import text_width
                cur += " " * max(wraps[i] - text_width(texts[i]), 0)
                continue
            rows.append(cur)
            cur = None
        if cur is not None:
            rows.append(cur)
    return rows


def sanitize(raw):
    return subprocess.run(["perl", SAN], input=raw, capture_output=True, check=True).stdout.decode("utf-8", "replace")


def forms(name, exact_raw, inferred_raw, width, reg=None):
    """the same session as exact (integration), inferred (no integration), capture-pane and formatted .log"""
    out = []
    if exact_raw is not None:
        sc = {"name": name + "|exact", "pipe": "raw", "data": base64.b64encode(exact_raw).decode(), "width": width}
        if reg is not None:
            sc["reg"] = reg
        out.append(sc)
    if inferred_raw is not None:
        out.append({"name": name + "|inferred", "pipe": "raw", "data": base64.b64encode(inferred_raw).decode(),
                    "width": width})
    src = exact_raw if exact_raw is not None else inferred_raw
    cap = ("\n".join(screen_rows_joined(src, width)) + "\n").encode()
    out.append({"name": name + "|capture", "pipe": "capture", "data": base64.b64encode(cap).decode(),
                "width": width})
    out.append({"name": name + "|log", "pipe": "clean", "data": sanitize(src), "width": width})
    return out


# ---------------------------------------------------------------- Kali's default zsh (2-line prompt, RPROMPT " N ⨯")
def kali_top(user=b"user", cwd=b"~", root=False):
    c = b"\x1b[34m" if root else b"\x1b[32m"
    u = b"\x1b[31m" if root else b"\x1b[34m"
    return (c + b"\xe2\x94\x8c\xe2\x94\x80\xe2\x94\x80(\x1b[1m" + u + user + b"\xe3\x89\xbfhost\x1b[0m" + c + b")-[\x1b[0m"
            b"\x1b[1m\x1b[39m" + cwd + b"\x1b[0m" + c + b"]\x1b[39m")


def kali_bot(root=False):
    c = b"\x1b[34m" if root else b"\x1b[32m"
    return c + b"\xe2\x94\x94\xe2\x94\x80\x1b[1m" + (b"\x1b[31m#" if root else b"\x1b[34m$") + b"\x1b[0m\x1b[39m "


class KaliRP:
    """Kali .zshrc: RPROMPT=$'%(?.. %? %F{red}%B⨯%b%F{reset})%(1j. %j %F{yellow}%B⚙%b%F{reset}.)'"""

    def __init__(self):
        self.jobs = 0

    def __call__(self, ec):
        s, w = b"", 0
        if ec:
            n = b"%d" % ec
            s += b" " + n + b" \x1b[31m\x1b[1m\xe2\xa8\xaf\x1b[22m\x1b[39m"
            w += 3 + len(n)
        if self.jobs:
            n = b"%d" % self.jobs
            s += b" " + n + b" \x1b[33m\x1b[1m\xe2\x9a\x99\x1b[22m\x1b[39m"
            w += 3 + len(n)
        return s, w


class ZK(ZR):
    """Kali's default zsh: precmd() sets the title and prints an empty line before every prompt but the first
    (NEWLINE_BEFORE_PROMPT=yes); 2-line prompt; RPROMPT on the bottom row."""

    def __init__(self, *a, root=False, kali_nl=True, **k):
        super().__init__(*a, **k)
        self.root = root
        self.kfirst = True
        self.kali_nl = kali_nl
        self.pre = self._kali_precmd

    def _kali_precmd(self):
        user = b"root" if self.root else b"user"
        s = b"\x1b]0;" + user + b"@host: " + self.cwd + b"\x07"
        if self.kali_nl and not self.kfirst:
            s += b"\r\n"
        self.kfirst = False
        return s

    def left(self):
        self.clock.next()
        b = kali_top(user=b"root" if self.root else b"user", cwd=self.cwd, root=self.root) + b"\r\n" + kali_bot(self.root)
        self.last_left = b
        return b

    def left_w(self):
        last = self.last_left.split(b"\r\n")[-1]
        return len(_plain(last))


# ---------------------------------------------------------------- bash 5.2 / readline 8.2 (+ emaki-integration.bash)
def _tag(n, k):
    return b";emaki=" + n.encode() + (b":%d" % k if k is not None else b"")


def _osc(x):
    return b"\x1b]" + x + b"\x07"


class BashS:
    """bash pane.  PROMPT_COMMAND (integration: D, OSC 7, A; Kali: empty line before every prompt but the first),
    readline: ESC[?2004h BEFORE PS1 (+B at its end), typed, CR LF, ESC[?2004l, PS0 (integration: C), output."""

    def __init__(self, ps1, nonce=None, kali_nl=False, cwd=b"/home/user/projects/notes-api", off=RL_OFF82):
        self.ps1 = ps1
        self.n = nonce
        self.kali_nl = kali_nl
        self.cwd = cwd
        self.off = off
        self.first = True
        self.seq = 0
        self.ec = 0
        self.s = b""

    def prompt(self):
        s = b""
        if self.n:
            if not self.first:
                s += _osc(b"133;D;%d" % self.ec + _tag(self.n, self.seq))
            s += _osc(b"7;file://host" + self.cwd) + _osc(b"133;A" + _tag(self.n, self.seq))
        if self.kali_nl and not self.first:
            s += b"\r\n"
        self.first = False
        s += BP_ON + self.ps1 + (_osc(b"133;B;emaki=" + self.n.encode()) if self.n else b"")
        self.s += s
        return self

    def accept(self, typed, out=b"", ec=0):
        self.s += typed + b"\r\n" + self.off
        if self.n and typed.strip():
            self.seq += 1
            self.s += _osc(b"133;C" + _tag(self.n, self.seq))
        if typed.strip():
            self.ec = ec
        self.s += out
        return self

    def run(self, typed, out=b"", ec=0):
        return self.prompt().accept(typed, out, ec)

    def ctrl_c(self, typed=b""):
        self.prompt()
        self.s += typed + b"^C" + self.off + b"\r\n"
        self.ec = 130
        return self

    def raw(self, b):
        self.s += b
        return self


# ---------------------------------------------------------------- p10k 2-line prompts (status/took on the FIRST row)
class ZP2(ZR):
    """p10k 2-line prompt: the first row's right segments (status ✘ N, took Ns) are part of PROMPT (padded with spaces
    for lean, with ─ for frame, up to the last column but one); the second row is "❯ " (lean) or "╰─❯ " (frame); no
    RPROMPT.  sparse=True: POWERLEVEL9K_PROMPT_ADD_NEWLINE (an empty line at the start of PROMPT)."""

    def __init__(self, *a, style="lean", sparse=False, status=None, **k):
        super().__init__(*a, **k)
        self.style = style
        self.sparse = sparse
        self.status = status     # callable(ec) -> (bytes, width), e.g. RP()
        self.nfirst = True

    def left(self):
        import lib_audit as LA
        hms, fr = self.clock.next()
        lt = LA.seg_date(b"2026-10-29") + b" " + LA.seg_time(LA.time_text("colon", hms, fr), b"") + b" " + \
            LA.seg_host(self.host) + b" " + LA.seg_cwd(self.cwd)
        lw = len(_plain(lt))
        rp, rpw = self.status(self.last_ec) if self.status else (b"", 0)
        if self.style == "frame":
            head, lw = b"\x1b[38;5;244m\xe2\x95\xad\xe2\x94\x80\x1b[39m " + lt, lw + 3
            fillc = b"\xe2\x94\x80"
            tailc, tw = b"\x1b[38;5;244m\xe2\x94\x80\xe2\x95\xae\x1b[39m", 2
            bot = b"\x1b[38;5;244m\xe2\x95\xb0\xe2\x94\x80\x1b[39m" + LA.SEG_ARROW
        else:
            head, fillc, tailc, tw = lt, b" ", b"", 0
            bot = LA.SEG_ARROW
        room = self.cols - 1 - lw - tw
        if rp and room >= rpw + 2:
            top = head + b" " + fillc * (room - rpw - 2) + b" " + rp + tailc
        elif self.style == "frame" and room >= 0:
            top = head + b" " + fillc * max(room - 1, 0) + tailc
        else:
            top = head
        b = (b"\r\n" if (self.sparse and not self.nfirst) else b"") + top + b"\r\n" + bot
        self.nfirst = False
        self.last_left = b
        return b

    def left_w(self):
        return len(_plain(self.last_left.split(b"\r\n")[-1]))
