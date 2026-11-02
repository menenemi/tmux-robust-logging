"""The limitations left after the background-job fix, one scenario each, exact and inferred.

  pre2        typed "gi", 2 ping lines arrive, typed "t status" + Enter            (typed prefix + bg + rest)
  pre1        typed "git st", ONE bg line arrives, typed "atus" + Enter            (no other bg row to compare with)
  pre-prev    same as pre1, but the job printed earlier lines at an idle prompt    (earlier bg rows to compare with)
  pre-all     typed all of "git status", 2 bg lines, Enter only
  pre-none    typed nothing, "ERROR [app] GET /x" + "[app] GET /y" arrive, then "ls" (bg row with its own prefix:
              must NOT be split as typed "ERROR ")
  pre-end     typed "make deplo", 2 bg lines, log ends (input never run)
  nonl1       bg prints "[app] tick 1\\r\\n[app] tick 2" (no newline at the end), user types "ls" + Enter
  nonl2       bg prints "Downloading... 50%" (no newline) at the idle prompt, user types "ls" + Enter
  mk1         first row: "make: *** ..." bg line, then "make test" typed after it      (shares "make" with E)
  mk2         for> row: same at a continuation prompt
  blank       pytest -q summary with blank lines at the idle prompt, then a command
  ml-bg       a 3-line buffer recalled with Up while a job runs; a bg line arrives under it; Enter
  ml-bg2      a 3-line buffer recalled with Up while a job runs, Enter (no bg line)

usage: python gen_lim.py -> fixtures/lim.json
"""
import base64
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from sesslib import Sess, N1, ZACC, ZIN  # noqa: E402
from lib_audit import G, preexec  # noqa: E402

scen = []


def add(name, raw, width=100):
    scen.append({"name": name, "pipe": "raw", "data": base64.b64encode(raw).decode(), "width": width})


PING = [b"64 bytes from 10.0.0.1: icmp_seq=%d ttl=64 time=0.05%d ms" % (k, k) for k in (1, 2)]
FOR3 = b"for f in *.log; do\n  gzip \"$f\"\ndone"
FOR3_DRAW = G(b"for") + b" f in *.log; " + G(b"do") + b"\r\r\n" + b"  " + G(b"gzip") + b" \"$f\"" + b"\r\r\n" + G(b"done")

for nonce, mode in ((N1, "exact"), (None, "inferred")):
    def sess(job=b"ping 10.0.0.1 &"):
        S = Sess(nonce=nonce)
        S.run(job, b"[1] 4321\r\n")
        return S, S.z

    S, z = sess()
    z.prompt().type(b"gi").raw(b"".join(x + b"\r\n" for x in PING))
    z.accept(b"git status", typed=b"t status").out(b"On branch main\r\n")
    z.prompt()
    add("pre2|" + mode, z.s)

    S, z = sess(b"./serve.sh &")
    z.prompt().type(b"git st").raw(b"[app] request handled in 12ms\r\n")
    z.accept(b"git status", typed=b"atus").out(b"On branch main\r\n")
    z.prompt()
    add("pre1|" + mode, z.s)

    S, z = sess(b"./serve.sh &")
    z.prompt().raw(b"[app] request handled in 9ms\r\n[app] request handled in 7ms\r\n")
    z.accept(b"ls", typed=b"ls").out(b"notes.txt\r\n")
    z.prompt().type(b"git st").raw(b"[app] request handled in 12ms\r\n")
    z.accept(b"git status", typed=b"atus").out(b"On branch main\r\n")
    z.prompt()
    add("pre-prev|" + mode, z.s)

    S, z = sess()
    z.prompt().type(b"git status").raw(b"".join(x + b"\r\n" for x in PING))
    z.accept(b"git status", typed=b"").out(b"On branch main\r\n")
    z.prompt()
    add("pre-all|" + mode, z.s)

    S, z = sess(b"./serve.sh &")
    z.prompt().raw(b"ERROR [app] GET /x 500\r\n[app] GET /y 200\r\n")
    z.accept(b"ls").out(b"notes.txt\r\n")
    z.prompt()
    add("pre-none|" + mode, z.s)

    S, z = sess(b"tail -f app.log &")
    z.prompt().type(b"make deplo").raw(b"[app] GET /notes 200\r\n[app] GET /health 200\r\n")
    add("pre-end|" + mode, z.s)

    S, z = sess(b"./watch.sh &")
    z.prompt().raw(b"[app] tick 1\r\n[app] tick 2")
    z.accept(b"ls").out(b"notes.txt\r\n")
    z.prompt()
    add("nonl1|" + mode, z.s)

    S, z = sess(b"./fetch.sh &")
    z.prompt().raw(b"Downloading... 50%")
    z.accept(b"ls").out(b"notes.txt\r\n")
    z.prompt()
    add("nonl2|" + mode, z.s)

    S, z = sess(b"make all >/dev/null &")
    z.prompt().raw(b"make: *** [Makefile:12: all] Error 2\r\n").accept(b"make test").out(b"ok\r\n")
    z.prompt()
    add("mk1|" + mode, z.s)

    S, z = sess(b"make all >/dev/null &")
    z.prompt()
    s = b"for d in api web; do" + ZACC
    s += b"for> " + ZIN + b"make: *** [Makefile:12: all] Error 2\r\n" + b"make test -C $d" + ZACC
    s += b"for> " + ZIN + b"done" + ZACC
    if nonce:
        z.seq += 1
        s += preexec(nonce, z.seq, b"for d in api web; do\nmake test -C $d\ndone")
    z.running = True
    z.raw(s).out(b"ok\r\nok\r\n")
    z.prompt()
    add("mk2|" + mode, z.s)

    S, z = sess(b"watchexec -e py -- pytest -q &")
    z.prompt().raw(b"[Running: pytest -q]\r\n..F.\r\n\r\n=== FAILURES ===\r\nE  assert 500 == 201\r\n\r\n"
                   b"1 failed, 3 passed in 0.31s\r\n")
    z.accept(b"git diff --stat", typed=G(b"git") + b" diff --stat").out(b" app/main.py | 2 +-\r\n")
    z.prompt()
    add("blank|" + mode, z.s)

    S, z = sess(b"./watch.sh &")
    z.prompt().raw(FOR3_DRAW + b"[app] tick 1\r\n").accept(FOR3, typed=b"").out(b"")
    z.prompt()
    add("ml-bg|" + mode, z.s)

    S, z = sess(b"./watch.sh &")
    z.prompt().raw(FOR3_DRAW).accept(FOR3, typed=b"").out(b"")
    z.prompt()
    add("ml-bg2|" + mode, z.s)

    # typed "gi" with a grey autosuggestion, a bg line, the suggestion accepted (zle goes back 2 columns and redraws
    # the whole buffer on the new row), Enter
    S, z = sess(b"./watch.sh &")
    z.prompt().raw(b"\x1b[36mgi\x1b[39m\x1b[38;5;8mt status --short\x1b[39m\x1b[16D[app] tick 1\r\n"
                   b"\x1b[2D\x1b[36mgit\x1b[39m status --short")
    z.accept(b"git status --short", typed=b"").out(b" M app/main.py\r\n")
    z.prompt()
    add("as-accept|" + mode, z.s)

json.dump(scen, open(os.path.join(HERE, "fixtures", "lim.json"), "w", encoding="utf-8"), ensure_ascii=False)
print(len(scen), "scenarios")
