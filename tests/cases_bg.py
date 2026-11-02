"""Background job output written while zsh waits at a prompt (no redraw): the typed command must stay the command,
and every background line must be kept as output, in both modes.

usage: python cases_bg.py <plugin dir> [-v]
"""
import os
import sys

PLUGIN = sys.argv[1]
VERBOSE = "-v" in sys.argv
sys.path.insert(0, PLUGIN)
import emaki.parse  # noqa: E402,F401
import emaki.render  # noqa: E402,F401
import emaki.report  # noqa: E402,F401
import emaki.segment  # noqa: E402,F401
import emaki.timefmt  # noqa: E402,F401

assert os.path.abspath(emaki.segment.__file__).startswith(os.path.abspath(PLUGIN)), emaki.segment.__file__
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lib_audit import Z, N1, run_raw, ZACC, ZIN, preexec  # noqa: E402

FAILS = []
COUNT = [0]


def check(name, cond, detail=""):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
    if VERBOSE or not cond:
        print(("PASS " if cond else "FAIL ") + name + (("  :: " + detail) if detail and not cond else ""))


def rows_of(d):
    return [r if isinstance(r, str) else "".join(x[0] for x in r) if isinstance(r, list) else "" for r in d.get("out", [])]


def summary(js):
    return [(d["kind"], d.get("cmd"), rows_of(d)) for d in js]


def cmds(js):
    return [d.get("cmd") for d in js if d["kind"] == "cmd"]


def all_rows(js):
    out = []
    for d in js:
        out += rows_of(d)
        for k in d.get("children", []):
            out += rows_of(k)
    return out


def modes():
    return (("exact", N1), ("inferred", None))


def bg_lines(n, tag="app"):
    return [b"[%s] tick %d" % (tag.encode(), k) for k in range(1, n + 1)]


def start(n, two=None, cols=100):
    z = Z("colon", nonce=n, two_line=two, cols=cols)
    z.prompt().accept(b"./watch.sh &").out(b"[1] 4321\r\n")
    return z


# 1. idle prompt, N background lines, then the user types a command
for nbg in (1, 2, 3, 5, 30):
    for two in (None, "lean", "frame"):
        for mode, n in modes():
            z = start(n, two)
            bg = bg_lines(nbg)
            z.prompt().raw(b"".join(x + b"\r\n" for x in bg)).accept(b"ls -la").out(b"notes.txt\r\n")
            z.prompt().accept(b"kill %1").out(b"")
            z.prompt()
            js, m, _, _ = run_raw(z.s)
            tag = "idle %d bg %s %s" % (nbg, two or "1line", mode)
            check(tag + " commands", cmds(js) == ["./watch.sh &", "ls -la", "kill %1"], repr(summary(js))[:600])
            rows = all_rows(js)
            check(tag + " bg lines kept in order", [r for r in rows if r.startswith("[app]")] == [x.decode() for x in bg],
                  repr(rows)[:600])
            ls = [d for d in js if d.get("cmd") == "ls -la"]
            check(tag + " ls output", bool(ls) and rows_of(ls[0]) == ["notes.txt"], repr(summary(js))[:600])

# 2. the background output starts with a newline (the cursor was already at the line start)
for mode, n in modes():
    z = start(n)
    z.prompt().raw(b"\r\n[app] tick 1\r\n[app] tick 2\r\n").accept(b"ls -la").out(b"notes.txt\r\n")
    z.prompt()
    js, m, _, _ = run_raw(z.s)
    check("leadnl %s commands" % mode, cmds(js) == ["./watch.sh &", "ls -la"], repr(summary(js)))
    check("leadnl %s bg kept" % mode, [r for r in all_rows(js) if r.startswith("[app]")] == ["[app] tick 1", "[app] tick 2"],
          repr(summary(js)))

# 3. idle prompt, background lines, then an empty Enter / Ctrl-C, then a real command at the next prompt
for how in ("enter", "ctrl-c"):
    for mode, n in modes():
        z = start(n)
        z.prompt().raw(b"[app] tick 1\r\n[app] tick 2\r\n")
        if how == "enter":
            z.empty_enter()
        else:
            z.ctrl_c()
        z.prompt().accept(b"ls -la").out(b"notes.txt\r\n")
        z.prompt()
        js, m, _, _ = run_raw(z.s)
        check("%s %s commands" % (how, mode), cmds(js) == ["./watch.sh &", "ls -la"], repr(summary(js)))
        check("%s %s bg kept" % (how, mode),
              [r for r in all_rows(js) if r.startswith("[app]")] == ["[app] tick 1", "[app] tick 2"], repr(summary(js)))

# 4. the log ends while the background job keeps printing at an idle prompt: no command may be invented
for mode, n in modes():
    z = start(n)
    z.prompt().raw(b"[app] tick 1\r\n[app] tick 2\r\n[app] tick 3\r\n")
    js, m, _, _ = run_raw(z.s)
    check("log-ends %s commands" % mode, cmds(js) == ["./watch.sh &"], repr(summary(js)))
    check("log-ends %s bg kept" % mode,
          [r for r in all_rows(js) if r.startswith("[app]")] == ["[app] tick 1", "[app] tick 2", "[app] tick 3"],
          repr(summary(js)))

# 5. a job-done notice (zle redraws the prompt) after background lines, then the command
for mode, n in modes():
    z = start(n)
    z.prompt().raw(b"[app] tick 1\r\n[app] tick 2\r\n").notice(b"[1]  + done       ./watch.sh")
    z.accept(b"ls -la").out(b"notes.txt\r\n")
    z.prompt()
    js, m, _, _ = run_raw(z.s)
    check("notice %s commands" % mode, cmds(js) == ["./watch.sh &", "ls -la"], repr(summary(js)))
    check("notice %s bg kept" % mode,
          [r for r in all_rows(js) if r.startswith("[app]")] == ["[app] tick 1", "[app] tick 2"], repr(summary(js)))

# 6. background lines that look like prompts or contain "❯ " / "$ " (a tail of another pane's log)
for mode, n in modes():
    z = start(n)
    bg = [b"2026-10-29 21:40:10 host ~ \xe2\x9d\xaf make", b"deploy@web01:~$ uptime", b"$ ls"]
    z.prompt().raw(b"".join(x + b"\r\n" for x in bg)).accept(b"ls -la").out(b"notes.txt\r\n")
    z.prompt()
    js, m, _, _ = run_raw(z.s)
    check("promptlike %s commands" % mode, cmds(js) == ["./watch.sh &", "ls -la"], repr(summary(js)))
    check("promptlike %s bg kept" % mode, all(x.decode() in all_rows(js) for x in bg), repr(summary(js)))

# 7. a narrow pane (the prompt wraps) and an F6-width pane
for cols in (20, 28, 40):
    for mode, n in modes():
        z = start(n, cols=cols)
        z.prompt().raw(b"[app] tick 1\r\n[app] tick 2\r\n").accept(b"ls -la").out(b"notes.txt\r\n")
        z.prompt()
        js, m, _, _ = run_raw(z.s, width=cols)
        check("narrow w=%d %s commands" % (cols, mode), cmds(js) == ["./watch.sh &", "ls -la"], repr(summary(js)))
        check("narrow w=%d %s bg kept" % (cols, mode),
              [r for r in all_rows(js) if r.startswith("[app]")] == ["[app] tick 1", "[app] tick 2"],
              repr(summary(js)))

# 8. typed the whole command, background line, then Enter with nothing more typed (exact: E knows the command)
z = start(N1)
z.prompt().raw(b"git status[app] request handled\r\n").accept(b"git status", typed=b"").out(b"On branch main\r\n")
z.prompt()
js, m, _, _ = run_raw(z.s)
check("typed-then-bg exact commands", cmds(js) == ["./watch.sh &", "git status"], repr(summary(js)))
check("typed-then-bg exact bg kept", "[app] request handled" in all_rows(js), repr(summary(js)))

# 9. background line at a dquote> continuation prompt (inferred: the continuation row is read from the screen)
for mode, n in modes():
    z = start(n)
    z.prompt()
    s = b'echo "a' + ZACC + b"dquote> " + ZIN + b"[app] request handled\r\n" + b'b"' + ZACC
    if n:
        z.seq += 1
        s += preexec(n, z.seq, b'echo "a\nb"')
    z.running = True
    z.raw(s).out(b"a\r\nb\r\n")
    z.prompt()
    js, m, _, _ = run_raw(z.s)
    check("ps2 %s commands" % mode, cmds(js) == ["./watch.sh &", 'echo "a\nb"'], repr(summary(js)))
    check("ps2 %s bg kept" % mode, "[app] request handled" in all_rows(js), repr(summary(js)))

# 10. typed part of the command, background lines (one with blank lines around it), then typed the rest and Enter.
#     exact: E has the whole command, so the rest on the accept row is input, not output
WALL = [b"", b"Broadcast message from root@devbox (Thu 2026-10-29 22:02:00 UTC):", b"", b"The system will reboot now!", b""]
for full, k in ((b"git status", 6), (b"git status", 4), (b"git status", 1), (b"ls -la /var/log", 3)):
    for two in (None, "lean", "frame"):
        for mode, n in modes():
            z = start(n, two)
            bg = [b"[app] tick 1"] + WALL + [b"[app] tick 2"]
            z.prompt().raw(full[:k] + b"".join(x + b"\r\n" for x in bg))
            z.accept(full, typed=full[k:]).out(b"done\r\n")
            z.prompt()
            js, m, _, _ = run_raw(z.s)
            tag = "split %r|%r %s %s" % (full[:k], full[k:], two or "1line", mode)
            rows = all_rows(js)
            check(tag + " bg kept", all(x.decode() in rows or full[:k].decode() + x.decode() in rows
                                        for x in bg if x), repr(summary(js))[:600])
            # exact: E has the command. inferred: the typed part is told apart from the background line by the other
            # background lines of the same job ("[app] tick 2" has the same shape as "[app] tick 1")
            check(tag + " commands", cmds(js) == ["./watch.sh &", full.decode()], repr(summary(js))[:600])
            check(tag + " no stray rest", full[k:].decode() not in rows, repr(summary(js))[:600])
            check(tag + " no typed text on the bg row", full[:k].decode() + "[app]" not in "".join(rows),
                  repr(summary(js))[:600])

print("%d checks, %d failed" % (COUNT[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
