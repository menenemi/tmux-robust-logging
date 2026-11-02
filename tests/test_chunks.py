"""Input and background output on one row, told apart by the read chunks of the .tsindex sidecar.

The scenarios are in fixtures/chunks.json (made by gen_chunks.py). Both modes unless noted.

usage: python test_chunks.py <plugin dir> [-v]
"""
import json
import os
import subprocess
import sys

PLUGIN = os.path.abspath(sys.argv[1])
VERBOSE = "-v" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
FAILS = []
COUNT = [0]
BG = "[app] request handled"


def check(name, cond, detail=""):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
    if VERBOSE or not cond:
        print(("PASS " if cond else "FAIL ") + name + (("  :: " + detail) if detail and not cond else ""))


out = os.path.join(HERE, "chunks.out.json")
r = subprocess.run([sys.executable, os.path.join(HERE, "runner.py"), PLUGIN, os.path.join(HERE, "fixtures", "chunks.json"),
                    out], env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1"), capture_output=True)
if r.returncode:
    sys.exit(r.stderr.decode("utf-8", "replace"))
res = json.load(open(out, encoding="utf-8"))
os.remove(out)


def blocks(n):
    assert "blocks" in res[n], res[n].get("error", "")[-800:]
    return res[n]["blocks"]


def t(name, cmd=None, has=(), hasnt=(), rows_only=None, modes=("exact", "inferred")):
    for mode in modes:
        n = name + "|" + mode
        bl = blocks(n)
        cmds = [b["cmd"] for b in bl if b["kind"] == "cmd"]
        rows = [x for b in bl for x in b.get("out", []) if isinstance(x, str)]
        s = repr([(b["kind"], b.get("cmd"), b.get("out")) for b in bl])[:700]
        if cmd is not None:
            check(n + " command", cmds[-1:] == [cmd], s)
        for x in has:
            check("%s has %r" % (n, x), x in rows, s)
        for x in hasnt:
            check("%s no %r" % (n, x), not any(x in y for y in rows), s)


t("a-one", "git status", ["[app] request handled in 12ms"], ["git st["])
t("a-upper", "Xvfb :1", ["[app] request handled in 12ms"], ["Xvf["])
t("a-split", "git status", [BG], ["git st["])
t("a-space", "ls -la", [BG], ["ls ["])
t("a-all", "git status", [BG], ["git status["])
t("a-none", "ls", [BG])
t("a-end", None, [BG], ["deplo["])
t("a-ctrlc", "ls", [BG], ["deplo["])
t("a-notice", "ls", ["[1]  + done       ./serve.sh"])  # the last key is not output just because it is the last chunk
check("a-notice|exact typed input stays whole", "git lo" in blocks("a-notice|exact")[1]["out"], repr(blocks("a-notice|exact")))
t("b-nonl", "ls", ["Downloading... 50%"], ["50%ls"])
t("b-nonl2", "ls", ["[app] tick 1", "[app] tick 2"], ["2ls"])
t("c-ml", 'for f in *.log; do\n  gzip "$f"\ndone', [BG], ["done["])
t("as-accept", "git status --short", [BG], ["gi["])
t("n-plain", "ls -la", [], [])
# a recalled or pasted command followed by typing stays one command: never lose a real command
t("r-recall", "git status -s")
t("r-old", "make deploy -j4")
t("r-paste", "echo hello x")
t("r-paste2", "[ -f notes ] && cat notes x")
# a tab-completed word is part of the command
t("a-tab", "git status", [BG], ["atus ["])
# known limit without shell integration: output that looks like a command start is left with the input
t("b-lower", "ls", ["downloading 50%"], modes=("exact",))
check("b-lower|inferred stays one command", [b["cmd"] for b in blocks("b-lower|inferred") if b["kind"] == "cmd"][-1:]
      == ["downloading 50%ls"])

print("%d checks, %d failed" % (COUNT[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
