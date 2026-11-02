"""The limitations fixed after the background-job fix: typed prefix before background output, background output
without a newline, a right prompt that is not a status, continuation rows, a prefix shared with E, blank lines in
background output, multi-line buffers, an accepted autosuggestion. Both modes unless noted.

usage: python cases_lim.py <plugin dir> [-v]
"""
import json
import os
import subprocess
import sys

PLUGIN = os.path.abspath(sys.argv[1])
VERBOSE = "-v" in sys.argv
HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "runner.py")
env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1")
FAILS = []
COUNT = [0]


def run(scen):
    out = os.path.join(HERE, "cases_lim.out.json")
    r = subprocess.run([sys.executable, RUNNER, PLUGIN, scen, out], env=env, capture_output=True)
    if r.returncode:
        sys.exit(r.stderr.decode("utf-8", "replace"))
    res = json.load(open(out, encoding="utf-8"))
    os.remove(out)
    return res


def check(name, cond, detail=""):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
    if VERBOSE or not cond:
        print(("PASS " if cond else "FAIL ") + name + (("  :: " + detail) if detail and not cond else ""))


def blocks(res, n):
    v = res[n]
    assert "blocks" in v, (n, v.get("error", "")[-600:])
    return v["blocks"]


def cmds(bl):
    return [b.get("cmd") for b in bl if b["kind"] == "cmd"]


def rows(bl):
    out = []
    for b in bl:
        out += [r for r in b.get("out", []) if isinstance(r, str)]
        out += rows(b.get("children") or [])
    return out


def misc_rows(bl):
    return [r for b in bl if b["kind"] == "misc" for r in b.get("out", []) if isinstance(r, str)]


FOR3 = 'for f in *.log; do\n  gzip "$f"\ndone'
PING = ["64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.051 ms", "64 bytes from 10.0.0.1: icmp_seq=2 ttl=64 time=0.052 ms"]
lim = run(os.path.join(HERE, "fixtures", "lim.json"))
for mode in ("exact", "inferred"):
    def t(name, last_cmd=None, has=(), hasnt=(), cmdlist=None):
        bl = blocks(lim, name + "|" + mode)
        r = rows(bl)
        s = repr([(b["kind"], b.get("cmd"), b.get("out")) for b in bl])[:700]
        if last_cmd is not None:
            check("%s|%s command" % (name, mode), cmds(bl)[-1:] == [last_cmd], s)
        if cmdlist is not None:
            check("%s|%s commands" % (name, mode), cmds(bl) == cmdlist, s)
        for x in has:
            check("%s|%s has %r" % (name, mode, x), x in r, s)
        for x in hasnt:
            check("%s|%s no %r" % (name, mode, x), not any(x in y for y in r), s)

    t("pre2", "git status", PING, ["gi64"])
    t("pre-prev", "git status", ["[app] request handled in 12ms"], ["git st[app]"])
    t("pre-all", "git status", PING, ["git status64"])
    t("pre-none", "ls", ["ERROR [app] GET /x 500", "[app] GET /y 200"])
    t("pre-end", None, ["[app] GET /notes 200", "[app] GET /health 200"], ["deplo[app]"],
      cmdlist=["tail -f app.log &"])
    t("nonl1", "ls", ["[app] tick 1", "[app] tick 2"], ["2ls"])
    t("mk1", "make test", ["make: *** [Makefile:12: all] Error 2"])
    t("mk2", "for d in api web; do\nmake test -C $d\ndone", ["make: *** [Makefile:12: all] Error 2"])
    t("ml-bg2", FOR3, cmdlist=["./watch.sh &", FOR3])
    t("as-accept", "git status --short", ["[app] tick 1hort"], ["gi[app]"])
    bl = blocks(lim, "pre-end|" + mode)
    check("pre-end|%s typed input kept as its own row" % mode, any(r.endswith("❯ make deplo") for r in misc_rows(bl)),
          repr(misc_rows(bl)))
    bl = blocks(lim, "blank|" + mode)
    check("blank|%s blank lines kept" % mode, misc_rows(bl) == [
        "[Running: pytest -q]", "..F.", "", "=== FAILURES ===", "E  assert 500 == 201", "", "1 failed, 3 passed in 0.31s"],
        repr(misc_rows(bl)))
    if mode == "exact":  # E tells what was typed
        t("pre1", "git status", ["[app] request handled in 12ms"], ["git st[app]"])
        t("nonl2", "ls", ["Downloading... 50%"])
        t("ml-bg", FOR3, ["[app] tick 1"], ["done[app]"])

s4 = run(os.path.join(HERE, "fixtures", "s4.json"))
for n in ("W2-jobsrp-idle-bg-end", "W3-jobsrp-typed-bg-enter"):
    r = rows(blocks(s4, n))
    check(n + " no jobs rprompt on the bg row", "[app] tick 1" in r and not any("✦" in x for x in r), repr(r))
s8 = run(os.path.join(HERE, "fixtures", "s8.json"))
for n in ("K2-kali-jobs-idle-bg-end", "K6-kali-jobs-typed-bg-enter"):
    r = rows(blocks(s8, n))
    check(n + " no Kali rprompt on the bg row", not any("⚙" in x for x in r), repr(r))
for n in ("P1-ps2-bg-shares-prefix", "P2-first-row-bg-shares-prefix"):
    r = rows(blocks(s8, n))
    check(n + " bg line keeps its prefix", "make: *** [Makefile:12: all] Error 2" in r, repr(r))
s9 = run(os.path.join(HERE, "fixtures", "s9.json"))
r = rows(blocks(s9, "T9-ps2-idle-bg-time"))
check("T9 ps2 bg line keeps its time", "[app]   tick   21:40:12" in r, repr(r))
r = rows(blocks(s9, "T9-ps2-idle-bg-dur"))
check("T9 ps2 bg line keeps its duration", "GET /api/notes   200   3.2s" in r, repr(r))
misc = run(os.path.join(HERE, "fixtures", "misc.json"))
for mode in ("exact", "inferred"):
    r = rows(blocks(misc, "ps2-bgtime|" + mode))
    check("ps2-bgtime|%s keeps the time" % mode, "[app]   tick   21:40:12" in r, repr(r))
probe = run(os.path.join(HERE, "fixtures", "probe1.json"))
for n, want in (("ml2|inferred", "sudo apt update\nsudo apt install -y jq"), ("ml3|inferred", FOR3)):
    check(n + " multi-line buffer is one command", want in cmds(blocks(probe, n)), repr(cmds(blocks(probe, n))))
c = cmds(blocks(probe, "isearch|inferred"))
check("Ctrl-R without shell integration: the found command", c == ["git log --oneline -1"] * 2, repr(c))
# the last row of a completion list shares its logical line with the redrawn input row
tab = run(os.path.join(HERE, "fixtures", "tab.json"))
for mode in ("exact", "inferred"):
    bl = blocks(tab, "%s tab 4-row list (/etc/ssh), more typing, Enter" % mode)
    check(mode + " list: last row whole", "sshd_config               ssh_host_ed25519_key" in misc_rows(bl), repr(misc_rows(bl)))
    check(mode + " list: command continues after the list", cmds(bl)[-1:] == ["cat /etc/ssh/sshd_config"], repr(cmds(bl)))

print("%d checks, %d failed" % (COUNT[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
