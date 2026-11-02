"""Targeted checks for the four misdetections (F1 F2 P20 F6) plus the cases they must not break.

usage: python cases.py <plugin dir> [-v]
Prints one line per check (PASS/FAIL) and a summary; exit 1 if anything failed.
"""
import os
import re
import sys

PLUGIN = sys.argv[1]
VERBOSE = "-v" in sys.argv
sys.path.insert(0, PLUGIN)
import emaki.parse  # noqa: E402,F401  (import the plugin under test BEFORE lib_audit puts the live one on sys.path)
import emaki.render  # noqa: E402,F401
import emaki.report  # noqa: E402,F401
import emaki.segment  # noqa: E402,F401
import emaki.timefmt  # noqa: E402,F401

assert os.path.abspath(emaki.segment.__file__).startswith(os.path.abspath(PLUGIN)), emaki.segment.__file__
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lib_audit import (Z, N1, run_raw, run_capture, run_clean, screen_rows, rl, BP_ON, RL_OFF81,  # noqa: E402
                       COLS)

FAILS = []
COUNT = [0]


def plain(b):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]|\x1b[=>]", "", b.decode("utf-8"))


def check(name, cond, detail=""):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
    if VERBOSE or not cond:
        print(("PASS " if cond else "FAIL ") + name + (("  :: " + detail) if detail and not cond else ""))


def brief(js):
    out = []
    for d in js:
        rows = ["".join(x[0] for x in r) if isinstance(r, list) else r if isinstance(r, str) else repr(r)
                for r in d.get("out", [])]
        out.append((d["kind"], d.get("cmd"), d.get("prompt"), d.get("exit"), rows))
    return out


def cmds(js):
    return [d.get("cmd") for d in js if d["kind"] == "cmd"]


def rprompt_fail(ec):
    s = b"INT" if ec == 130 else b"%d" % ec
    return (b"\x1b[31m\xe2\x9c\x98 " + s + b"\x1b[39m", 2 + len(s)) if ec else (b"", 0)


def rprompt_took(ec):
    return b"\x1b[33mtook 5s\x1b[39m", 7


def raw_modes():
    return (("exact", N1), ("inferred", None))


# ---------------------------------------------------------------- F1: typed text with 3+ spaces + a time-like token
F1_CMDS = [b"echo 'at     12:00'", b"printf '%s     %s\\n' start 21:40:05", b"echo 'step     3.5s'",
           b"echo 'x   \xe2\x9c\x94'", b"echo   took"]
F1_OUT = [b"at     12:00\r\n", b"start     21:40:05\r\n", b"step     3.5s\r\n", b"x   \xe2\x9c\x94\r\n", b"took\r\n"]
for mode, n in raw_modes():
    for form in ("colon", "compact"):
        z = Z(form, nonce=n)
        for c, o in zip(F1_CMDS, F1_OUT):
            z.prompt().accept(c).out(o)
        z.prompt()
        js, m, _, _ = run_raw(z.s)
        want = [c.decode() for c in F1_CMDS]
        check("F1 %s %s commands kept whole" % (mode, form), cmds(js) == want, repr(cmds(js)))
# (the same screen read back from a capture / a formatted .log - regex mode, no width, no redraw marks - still takes
#  such tails for a right prompt, as before the fix: nothing tells them apart there)

# regex mode with real right prompts in the pane (one, several, after a resize): still removed
def regex_both(rows):
    a = run_capture(("\n".join(rows) + "\n").encode())[0]
    b = run_clean("\n".join(rows) + "\n")[0]
    return a, b


for name, rpf, seq in (
        ("single fail", rprompt_fail, [(b"false", b"", 1), (b"ls", b"notes.txt\r\n", 0)]),
        ("single took", rprompt_took, [(b"sleep 5", b"", 0), (b"ls", b"notes.txt\r\n", 0)]),
        ("fails", rprompt_fail, [(b"false", b"", 1), (b"echo hi", b"hi\r\n", 0),
                                  (b"cat nope", b"cat: nope: No such file or directory\r\n", 1),
                                  (b"ls", b"notes.txt\r\n", 0)])):
    z = Z("colon", nonce=None, rprompt=rpf if name != "single took" else None)
    for i, (c, o, ec) in enumerate(seq):
        z.prompt()
        if name == "single took" and i == 1:  # "took 5s" only after the slow command
            z.s = z.s[:-len(b"\x1b[K\x1b[?1h\x1b=\x1b[?2004h")]
            rp, rpw = rprompt_took(0)
            pw = z.left_w()
            z.s += b"\x1b[K\x1b[%dC" % (COLS - 1 - rpw - pw) + rp + b"\r\x1b[%dC" % pw + b"\x1b[?1h\x1b=\x1b[?2004h"
        z.accept(c).out(o, ec=ec)
    z.prompt()
    rows = screen_rows(z.s)
    for how, js in zip(("capture", "formatted-log"), regex_both(rows)):
        want = [c.decode() for c, _, _ in seq]
        check("RPX %s %s commands" % (how, name), cmds(js) == want, repr(cmds(js)))
        if rpf is rprompt_fail:
            # without the terminal width, only a shown ✘ tells the exit code (no ✘ = unknown, or 0 if inferred)
            ex = [d.get("exit") for d in js if d["kind"] == "cmd"]
            want_ex = [ec for _, _, ec in seq][:-1]
            check("RPX %s %s exit codes" % (how, name),
                  len(ex) > len(want_ex) and all(e == w if w else e in (None, 0) for e, w in zip(ex, want_ex)),
                  repr(ex))
# the same pane after a resize: right prompts at two different right edges
z1 = Z("colon", nonce=None, rprompt=rprompt_fail, cols=100)
z1.prompt().accept(b"false").out(b"", ec=1)
z1.prompt().accept(b"cat nope").out(b"cat: nope: No such file\r\n", ec=1)
z1.prompt().accept(b"true").out(b"", ec=0)
z2 = Z("colon", nonce=None, rprompt=rprompt_fail, cols=80, clock=z1.clock)
z2.last_ec = 0
z2.prompt().accept(b"false").out(b"", ec=1)
z2.prompt().accept(b"cat nope").out(b"cat: nope: No such file\r\n", ec=1)
z2.prompt().accept(b"ls").out(b"notes.txt\r\n", ec=0)
z2.prompt()
rows = screen_rows(z1.s, 100) + screen_rows(z2.s, 80)
for how, js in zip(("capture", "formatted-log"), regex_both(rows)):
    check("RPX %s resized commands" % how,
          cmds(js) == ["false", "cat nope", "true", "false", "cat nope", "ls"], repr(cmds(js)))

# bash 2-line prompt (Kali's default bash PS1; readline puts the mark BEFORE the prompt): no right prompt in bash
top = b"\xe2\x94\x8c\xe2\x94\x80\xe2\x94\x80(user\xe3\x89\xbfhost)-[~]\r\n\xe2\x94\x94\xe2\x94\x80$ "
bash2 = b""
for c, o in zip(F1_CMDS, F1_OUT):
    bash2 += BP_ON + top + c + b"\r\n" + RL_OFF81 + o
bash2 += BP_ON + top
js, m, _, _ = run_raw(bash2)
check("F1 bash 2-line prompt commands kept whole", cmds(js) == [c.decode() for c in F1_CMDS], repr(cmds(js)))


# ---------------------------------------------------------------- real right prompts must still be removed
for mode, n in raw_modes():
    for name, rpf in (("fail", rprompt_fail), ("took", rprompt_took)):
        z = Z("colon", nonce=n, rprompt=rpf)
        z.prompt().accept(b"false").out(b"", ec=1)
        z.prompt().accept(b"cat nope").out(b"cat: nope: No such file or directory\r\n", ec=127)
        z.prompt().accept(b"echo 'at     12:00'").out(b"at     12:00\r\n", ec=0)
        z.prompt().accept(b"ls").out(b"notes.txt\r\n", ec=0)
        z.prompt()
        js, m, _, _ = run_raw(z.s)
        cs = cmds(js)
        check("RP %s %s commands" % (mode, name), cs == ["false", "cat nope", "echo 'at     12:00'", "ls"], repr(cs))
        if name == "fail":
            ex = [d.get("exit") for d in js if d["kind"] == "cmd"]
            want = [1, 127, 0, 0] if mode == "exact" else [1, 127, 0]
            check("RP %s fail exit codes" % mode, ex[:len(want)] == want, repr(ex))
        # nothing of the right prompt may end up in an output row
        allrows = [r for d in brief(js) for r in d[4]]
        check("RP %s %s no right prompt in output" % (mode, name),
              not any("\u2718" in r or "took 5s" in r for r in allrows), repr(allrows))
# right prompt with ZLE_RPROMPT_INDENT=0 (ends on the last column)
for mode, n in raw_modes():
    z = Z("colon", nonce=n)
    for c, ec in ((b"false", 1), (b"ls", 0)):
        z.prompt()
        if c == b"ls":
            # redraw the right prompt flush with the right edge
            z.s = z.s[:-len(b"\x1b[K\x1b[?1h\x1b=\x1b[?2004h")]
            rp = b"\x1b[31m\xe2\x9c\x98 1\x1b[39m"
            pw = z.left_w()
            z.s += b"\x1b[K\x1b[%dC" % (COLS - 3 - pw) + rp + b"\r\x1b[%dC" % pw + b"\x1b[?1h\x1b=\x1b[?2004h"
        z.accept(c).out(b"" if ec else b"notes.txt\r\n", ec=ec)
    z.prompt()
    js, m, _, _ = run_raw(z.s)
    ex = [d.get("exit") for d in js if d["kind"] == "cmd"]
    check("RP %s indent0 commands/exit" % mode, cmds(js) == ["false", "ls"] and ex[0] == 1, repr((cmds(js), ex)))

# the pane is resized while a prompt with a right prompt waits: zle redraws it at the new width (old PROMPT_SP width
# no longer matches), so the right prompt must still be recognised; typed look-alikes on such rows stay intact
def redraw_at(z, new_cols, rp, rpw):
    pw = z.left_w()
    return (b"\r\x1b[J" + z.last_left + b"\x1b[K" + b"\x1b[%dC" % (new_cols - 1 - rpw - pw) + rp
            + b"\r\x1b[%dC" % pw)


# (widening is not checked: emaki renders the row at the old PROMPT_SP width, so a right prompt redrawn beyond it
#  wraps in the rendering; that was already so before this change and is unchanged)
for mode, n in raw_modes():
    for new in (80, 60):
        z = Z("colon", nonce=n, rprompt=rprompt_fail)
        z.prompt().accept(b"false").out(b"", ec=1)
        z.prompt()
        rp, rpw = rprompt_fail(1)
        z.raw(redraw_at(z, new, rp, rpw))
        z.accept(b"ls").out(b"notes.txt\r\n", ec=0)
        z.prompt()
        z.accept(b"echo 'at     12:00'").out(b"at     12:00\r\n")
        z.prompt()
        js, m, _, _ = run_raw(z.s)
        ex = [d.get("exit") for d in js if d["kind"] == "cmd"]
        tag = "RESIZE %s ->%d" % (mode, new)
        check(tag + " commands", cmds(js) == ["false", "ls", "echo 'at     12:00'"], repr(cmds(js)))
        check(tag + " exit of the failed command", ex[:1] == [1], repr(ex))
# zsh without PROMPT_SP (setopt no_prompt_sp): the width is a guess, so right prompts are found as before the fix
for mode, n in raw_modes():
    z = Z("colon", nonce=n, rprompt=rprompt_fail)
    z.prompt().accept(b"false").out(b"", ec=1)
    z.prompt().accept(b"echo 'at     12:00'").out(b"at     12:00\r\n", ec=0)
    z.prompt().accept(b"ls").out(b"notes.txt\r\n")
    z.prompt()
    raw = z.s.replace(b" " * COLS + b"\r\r", b"")
    js, m, _, _ = run_raw(raw)
    ex = [d.get("exit") for d in js if d["kind"] == "cmd"]
    check("NOPSP %s commands" % mode, cmds(js) == ["false", "echo 'at     12:00'", "ls"], repr(cmds(js)))
    check("NOPSP %s exit of the failed command" % mode, ex[:1] == [1], repr(ex))

# ---------------------------------------------------------------- F2: background output written at the prompt
BG = b"[app]   tick   21:40:12"
for mode, n in raw_modes():
    for rp in (None, rprompt_fail):
        z = Z("colon", nonce=n, rprompt=rp)
        z.prompt().accept(b"tail -f /var/log/app.log &").out(b"[1] 4321\r\n", ec=0 if rp is None else 1)
        z.prompt().raw(BG + b"\r\n").accept(b"ls").out(b"notes.txt\r\n")
        z.prompt().accept(b"kill %1").out(b"")
        z.prompt()
        js, m, _, _ = run_raw(z.s)
        b = brief(js)
        tag = "F2 %s%s" % (mode, " +rprompt" if rp else "")
        check(tag + " commands", cmds(js) == ["tail -f /var/log/app.log &", "ls", "kill %1"], repr(cmds(js)))
        misc = [r for k, c, p, e, rs in b if k == "misc" for r in rs]
        check(tag + " background output kept as written", misc == [BG.decode()], repr(misc))
        if rp:
            ex = [d.get("exit") for d in js if d["kind"] == "cmd"]
            check(tag + " exit of the first command", ex[0] == 1, repr(ex))
# typed one character, then the background output, then the rest
for mode, n in raw_modes():
    z = Z("colon", nonce=n)
    z.prompt().accept(b"tail -f /var/log/app.log &").out(b"[1] 4321\r\n")
    z.prompt().raw(b"l" + BG + b"\r\n").accept(b"ls", typed=b"s").out(b"notes.txt\r\n")
    z.prompt()
    js, m, _, _ = run_raw(z.s)
    b = brief(js)
    misc = [r for k, c, p, e, rs in b if k == "misc" for r in rs]
    check("F2 %s typed-before: background output kept as written" % mode, any(BG.decode() in r for r in misc),
          repr(b))

# ---------------------------------------------------------------- P20: typed text containing a prompt-like "» " / "❯ "
for two in (None, "lean", "frame"):
    for mode, n in raw_modes():
        z = Z("colon", nonce=n, two_line=two)
        z.prompt().accept(b"ls").out(b"notes.txt\r\n")
        z.prompt()
        want_p = plain(z.last_left).replace("\r\n", "\n").rstrip()
        z.accept(b"echo 'a \xc2\xbb b'").out(b"a \xc2\xbb b\r\n")
        z.prompt()
        want_p2 = plain(z.last_left).replace("\r\n", "\n").rstrip()
        z.accept(b"echo '\xe2\x9d\xaf x' && echo '\xe2\x80\xba y'").out(b"\xe2\x9d\xaf x\r\n\xe2\x80\xba y\r\n")
        z.prompt()
        js, m, _, _ = run_raw(z.s)
        tag = "P20 %s %s" % (mode, two or "1line")
        cs = cmds(js)
        check(tag + " commands", cs == ["ls", "echo 'a \u00bb b'", "echo '\u276f x' && echo '\u203a y'"], repr(cs))
        ps = [d.get("prompt") for d in js if d["kind"] == "cmd"]
        check(tag + " prompt fields", len(ps) == 3 and ps[1] == want_p and ps[2] == want_p2,
              repr((ps, want_p, want_p2)))
        outs = [rs for k, c, p, e, rs in brief(js) if k == "cmd"]
        check(tag + " outputs", len(outs) == 3 and outs[1] == ["a \u00bb b"], repr(outs))

# bash readline (marks BEFORE PS1): output without a final newline, then the prompt — must still be split off
bash = (rl(b"user@host:~$ ", b"printf partial") + b"partial" + rl(b"user@host:~$ ", b"ls") + b"notes.txt\r\n"
        + BP_ON + b"user@host:~$ ")
js, m, _, _ = run_raw(bash)
b = brief(js)
check("bash dangle commands", cmds(js) == ["printf partial", "ls"], repr(b))
check("bash dangle output", b and b[0][4] == ["partial"] and b[1][2] == "user@host:~$", repr(b))

# ---------------------------------------------------------------- F6: prompt exactly one cell wider than the pane
FULL = {b"~": "2026-10-29TThost~❯", b"~/.tmux/plugins": "2026-10-29TThost~/.tmux/plugins❯"}


def norm(p):
    # a prompt that wraps keeps a line break where the terminal wrapped it: compare without any whitespace
    return re.sub(r"\s", "", re.sub(r"\d\d:?\d\d:?\d\d", "TT", re.sub(r"\n", "", p or "")))


for cwd, pw in ((b"~", 29), (b"~/.tmux/plugins", 43)):
    for cols in range(max(pw - 6, 12), pw + 4):
        for mode, n in raw_modes():
            z = Z("colon", nonce=n, cols=cols, cwd=cwd)
            z.prompt().accept(b"git status").out(b"On branch main\r\n")
            z.prompt().accept(b"ls").out(b"tmux-robust-logging\r\n")
            z.prompt()
            js, m, _, _ = run_raw(z.s, width=cols)
            tag = "F6 %s cwd=%s w=%d (prompt %d)" % (mode, cwd.decode(), cols, pw)
            check(tag + " commands", cmds(js) == ["git status", "ls"], repr(brief(js)))
            ps = [d.get("prompt") for d in js if d["kind"] == "cmd"]
            check(tag + " prompt text", all(norm(p) == FULL[cwd] for p in ps), repr(ps))
            check(tag + " no trailing newline", all(p and not p.endswith("\n") for p in ps), repr(ps))

# F6 width + a job-done notice while typing: zle redraws "prompt + typed so far" (wrapping the same way), then the
# rest is typed; the command must be the whole line
NOTE = b"[1]  + done       sleep 5"
for cwd, pw in ((b"~", 29), (b"~/.tmux/plugins", 43)):
    for cols in (pw - 1, pw - 2, pw, (pw - 1) // 2, 60):
        for mode, n in raw_modes():
            z = Z("colon", nonce=n, cols=cols, cwd=cwd)
            z.prompt().accept(b"sleep 5 &").out(b"[1] 4242\r\n")
            z.prompt().type(b"grep no").notice(NOTE, typed_again=b"grep no")
            z.accept(b"grep notes notes.txt", typed=b"tes notes.txt").out(b"notes here\r\n")
            z.prompt().accept(b"ls").out(b"notes.txt\r\n")
            z.prompt()
            js, m, _, _ = run_raw(z.s, width=cols)
            tag = "F6+notice %s cwd=%s w=%d" % (mode, cwd.decode(), cols)
            check(tag + " commands", cmds(js) == ["sleep 5 &", "grep notes notes.txt", "ls"], repr(brief(js)))

print("%d checks, %d failed" % (COUNT[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
