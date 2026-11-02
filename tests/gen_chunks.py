"""Scenarios with the read chunks of rl_tee.pl (.tsindex): each key is its own chunk, background output another.

  a-one       typed "git st", ONE bg line, typed "atus" + Enter
  a-upper     typed "Xvf", one bg line, typed "b :1" + Enter (a command that does not start lowercase)
  a-split     typed "git st", a bg line written in two pieces within a second, "atus" + Enter
  a-space     typed "ls ", one bg line, typed "-la" + Enter
  a-tab       typed "git st", TAB completes "atus " (one chunk), one bg line a few seconds later, Enter
  a-all       typed all of "git status", one bg line, Enter only
  a-none      typed nothing, one bg line, then "ls" + Enter
  a-end       typed "make deplo", one bg line, the log ends
  a-ctrlc     typed "make deplo", one bg line, Ctrl-C, then "ls"
  a-notice    typed "git lo", job notice (zle redraws), Ctrl-C, then "ls": nothing here is background output
  b-nonl      bg prints "Downloading... 50%" (no newline) at the idle prompt, "ls" + Enter
  b-nonl2     bg prints "[app] tick 1\\r\\n[app] tick 2" then "ls" + Enter
  b-lower     bg prints "downloading 50%" (lowercase, no newline), "ls" + Enter: cannot be told from a recalled command
  r-recall    Up recalls "git status" (run earlier in this log) while a job runs, typed " -s", Enter
  r-old       Up recalls "make deploy" (older history), typed " -j4", Enter
  r-paste     pasted "echo hello" (zle standout) while a job runs, typed " x", Enter
  r-paste2    pasted "[ -f notes ] && cat notes" (does not look like a command start), typed " x", Enter
  c-ml        a 3-line buffer drawn by zle, one bg line right of the last row, Enter
  as-accept   typed "gi", suggestion, bg line, suggestion accepted, Enter
  n-plain     a job runs, ordinary typed command, no interruption (control)

usage: python gen_chunks.py -> fixtures/chunks.json
"""
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sesslib import Sess, N1, ZACC  # noqa: E402
from lib_audit import G  # noqa: E402

scen = []
FOR3 = b"for f in *.log; do\n  gzip \"$f\"\ndone"
FOR3_DRAW = G(b"for") + b" f in *.log; " + G(b"do") + b"\r\r\n" + b"  " + G(b"gzip") + b" \"$f\"" + b"\r\r\n" + G(b"done")


class R:
    """records the chunk boundaries of a Sess the way rl_tee.pl does: "epoch cumulative-offset" per read"""

    def __init__(self, nonce, job=b"./serve.sh &"):
        self.S = Sess(nonce=nonce)
        self.z = self.S.z
        self.t = 1000
        self.recs = [(self.t, 0)]
        self.S.run(job, b"[1] 4321\r\n")
        self.mark()

    def mark(self, dt=3):
        self.t += dt
        self.recs.append((self.t, len(self.z.s)))

    def prompt(self):
        self.z.prompt()
        self.mark()
        return self

    def keys(self, b):
        for i in range(len(b)):  # several keys a second: the same epoch is fine, each is 1 visible char
            self.z.type(b[i:i + 1])
            self.mark(0)
        return self

    def out(self, b, dt=3):
        self.z.raw(b)
        self.mark(dt)
        return self

    def enter(self, cmd, then=b""):
        self.z.accept(cmd, typed=b"")
        self.mark(0)
        if then:
            self.z.out(then)
            self.mark()
        return self


def add(name, r, width=100):
    scen.append({"name": name, "pipe": "raw", "data": base64.b64encode(r.z.s).decode(), "width": width,
                 "ts": r.recs})


for nonce, mode in ((N1, "exact"), (None, "inferred")):
    m = "|" + mode
    r = R(nonce).prompt().keys(b"git st").out(b"[app] request handled in 12ms\r\n").keys(b"atus")
    add("a-one" + m, r.enter(b"git status", b"On branch main\r\n").prompt())

    r = R(nonce).prompt().keys(b"Xvf").out(b"[app] request handled in 12ms\r\n").keys(b"b :1")
    add("a-upper" + m, r.enter(b"Xvfb :1", b"started\r\n").prompt())

    r = R(nonce).prompt().keys(b"git st").out(b"[app] req", 0).out(b"uest handled\r\n", 0).keys(b"atus")
    add("a-split" + m, r.enter(b"git status", b"On branch main\r\n").prompt())

    r = R(nonce).prompt().keys(b"ls ").out(b"[app] request handled\r\n").keys(b"-la")
    add("a-space" + m, r.enter(b"ls -la", b"notes.txt\r\n").prompt())

    r = R(nonce).prompt().keys(b"git st").out(b"atus ").out(b"[app] request handled\r\n")
    add("a-tab" + m, r.enter(b"git status ", b"On branch main\r\n").prompt())

    r = R(nonce).prompt().keys(b"git status").out(b"[app] request handled\r\n")
    add("a-all" + m, r.enter(b"git status", b"On branch main\r\n").prompt())

    r = R(nonce).prompt().out(b"[app] request handled\r\n").keys(b"ls")
    add("a-none" + m, r.enter(b"ls", b"notes.txt\r\n").prompt())

    r = R(nonce).prompt().keys(b"make deplo").out(b"[app] request handled\r\n")
    add("a-end" + m, r)

    r = R(nonce).prompt().keys(b"make deplo").out(b"[app] request handled\r\n")
    r.z.ctrl_c()
    r.mark()
    r.prompt().keys(b"ls")
    add("a-ctrlc" + m, r.enter(b"ls", b"notes.txt\r\n").prompt())

    r = R(nonce).prompt().keys(b"git lo")
    r.z.notice(b"[1]  + done       ./serve.sh", typed_again=b"git lo")
    r.mark()
    r.z.ctrl_c()
    r.mark()
    r.prompt().keys(b"ls")
    add("a-notice" + m, r.enter(b"ls", b"notes.txt\r\n").prompt())

    r = R(nonce).prompt().out(b"Downloading... 50%").keys(b"ls")
    add("b-nonl" + m, r.enter(b"ls", b"notes.txt\r\n").prompt())

    r = R(nonce).prompt().out(b"[app] tick 1\r\n[app] tick 2").keys(b"ls")
    add("b-nonl2" + m, r.enter(b"ls", b"notes.txt\r\n").prompt())

    r = R(nonce).prompt().out(b"downloading 50%").keys(b"ls")
    add("b-lower" + m, r.enter(b"ls", b"notes.txt\r\n").prompt())

    r = R(nonce).prompt().keys(b"git status")
    r.enter(b"git status", b"On branch main\r\n").prompt().out(b"git status").keys(b" -s")
    add("r-recall" + m, r.enter(b"git status -s", b" M app.py\r\n").prompt())

    r = R(nonce).prompt().out(b"make deploy").keys(b" -j4")
    add("r-old" + m, r.enter(b"make deploy -j4", b"ok\r\n").prompt())

    r = R(nonce).prompt().out(b"\x1b[7mecho hello\x1b[27m").keys(b" x")
    add("r-paste" + m, r.enter(b"echo hello x", b"hello x\r\n").prompt())

    r = R(nonce).prompt().out(b"\x1b[7m[ -f notes ] && cat notes\x1b[27m").keys(b" x")
    add("r-paste2" + m, r.enter(b"[ -f notes ] && cat notes x", b"hello x\r\n").prompt())

    r = R(nonce).prompt().out(FOR3_DRAW).out(b"[app] request handled\r\n")
    add("c-ml" + m, r.enter(FOR3).prompt())

    r = R(nonce).prompt().keys(b"g").keys(b"i").out(b"\x1b[38;5;8mt status --short\x1b[39m\x1b[16D")
    r.out(b"[app] request handled\r\n").out(b"\x1b[2D\x1b[36mgit\x1b[39m status --short")
    add("as-accept" + m, r.enter(b"git status --short", b" M app.py\r\n").prompt())

    r = R(nonce).prompt().keys(b"ls -la")
    add("n-plain" + m, r.enter(b"ls -la", b"notes.txt\r\n").prompt())

json.dump(scen, open(os.path.join(HERE, "fixtures", "chunks.json"), "w", encoding="utf-8"), ensure_ascii=False)
print(len(scen), "scenarios")
