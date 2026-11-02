"""emaki --raw: HTML from log files, with no tmux at all.

usage: python test_raw_export.py <plugin dir> [-v]
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

PLUGIN = os.path.abspath(sys.argv[1])
VERBOSE = "-v" in sys.argv
sys.path.insert(0, PLUGIN)
import emaki.parse  # noqa: E402,F401
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from lib_audit import Z, N1  # noqa: E402

EMAKI = os.path.join(PLUGIN, "bin", "emaki")
DATA = re.compile(r'<script id="rl-data" type="application/json">(.*?)</script>', re.S)
FAILS = []
COUNT = [0]
T = tempfile.mkdtemp(prefix="emaki-raw-")
NONCES = os.path.join(T, "nonces")
ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", EMAKI_NONCE_FILE=NONCES,
           EMAKI_TMUX=os.path.join(T, "no-such-tmux"))  # tmux is not there at all
ENV.pop("TMUX", None)
ENV.pop("TMUX_PANE", None)


def check(name, cond, detail=""):
    COUNT[0] += 1
    if not cond:
        FAILS.append(name)
    if VERBOSE or not cond:
        print(("PASS " if cond else "FAIL ") + name + (("  :: " + detail) if detail and not cond else ""))


def emaki(*args):
    return subprocess.run([sys.executable, EMAKI] + list(args), env=ENV, capture_output=True)


def export(*files, **kw):
    out = os.path.join(T, kw.get("out", "out.html"))
    r = emaki("-q", "--raw", *(list(files) + ["-o", out]))
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    html = open(out, encoding="utf-8").read()
    return json.loads(DATA.search(html).group(1)), html


def cmds(data):
    return [b["cmd"] for b in data["blocks"] if b["kind"] == "cmd"]


def session(d, name, ts, pane=3, window="0-zsh", cmd=b"ls", nonce=N1, server="4242", index=True, text=b"notes.txt\r\n"):
    """a finished log: raw, and its index when asked"""
    os.makedirs(d, exist_ok=True)
    base = os.path.join(d, "tmux_%s_%s_p%d_%s" % (name, window, pane, ts))
    z = Z("colon", nonce=nonce)
    z.prompt().accept(cmd).out(text, ec=0)
    z.prompt().accept(b"false").out(b"", ec=1)
    z.prompt()
    with open(base + ".raw.log", "wb") as f:
        f.write(z.s)
    if index:
        os.makedirs(os.path.join(d, ".rlindex"), exist_ok=True)
        with open(os.path.join(d, ".rlindex", os.path.basename(base) + ".info"), "w", encoding="utf-8") as f:
            f.write("base=%s\nrawbase=%s\nparts=0\nconv=none\ntsfmt=none\nserver_pid=%s\nsession_id=$1\n"
                    "session_name=%s\nsession_created=1700000000\nwindow_index=0\nwindow_name=zsh\npane_id=%%%d\n"
                    "pane_index=0\nstart=1700000000\nuser=user\nhost=host\npane_width=100\n"
                    % (base, base, server, name, pane))
    return base


def register(nonce=N1, server="4242", pane="%3"):
    with open(NONCES, "a", encoding="utf-8") as f:
        f.write("1 %s %s %s 1700000000 1234\n" % (nonce, server, pane))


try:
    # 1. a finished session, with its index and the nonce registry: exact, with exit codes, no tmux
    register()
    b1 = session(os.path.join(T, "logs"), "work", "20261101T101010")
    data, html = export(b1 + ".raw.log")
    check("exact commands", cmds(data) == ["ls", "false"], repr(cmds(data)))
    check("exact exit codes", [b.get("exit") for b in data["blocks"] if b["kind"] == "cmd"] == [0, 1],
          repr(data["blocks"])[:400])
    check("session from the index", data["meta"]["session"] == "work" and data["meta"]["scope"] == "session",
          repr(data["meta"]))
    check("host from the index", data["meta"]["host"] == "host", repr(data["meta"]))
    check("no warning when marks are verified", not data["meta"]["notes"], repr(data["meta"]["notes"]))
    check("no external url", "http://" not in re.sub(r"http://www\.w3\.org/\S*", "", DATA.sub("", html))
          and "https://" not in DATA.sub("", html))

    # 2. marks whose nonce is not registered are not believed (forged, or a log from another machine)
    b2 = session(os.path.join(T, "other"), "work", "20261101T101011", server="9999")
    data, _ = export(b2 + ".raw.log")
    check("unverified: commands still split", cmds(data) == ["ls", "false"], repr(cmds(data)))
    check("unverified: no exit code from the marks", all(b.get("exit") in (None,) for b in data["blocks"]
                                                         if b["kind"] == "cmd"), repr(data["blocks"])[:400])
    check("unverified: says so", any("確かめられない" in n for n in data["meta"]["notes"]), repr(data["meta"]["notes"]))

    # 3. no index: the file name is enough
    b3 = session(os.path.join(T, "noindex"), "lab", "20261101T101012", pane=7, window="2-ops", index=False)
    data, _ = export(b3 + ".raw.log")
    check("no index: session from the name", data["meta"]["session"] == "lab", repr(data["meta"]))
    check("no index: pane label from the name", data["panes"][0]["label"].startswith("2:ops"), repr(data["panes"]))

    # 4. several panes of one session: one timeline
    d = os.path.join(T, "panes")
    register(pane="%4")
    p1 = session(d, "work", "20261101T101013", pane=3, window="0-zsh", cmd=b"id")
    p2 = session(d, "work", "20261101T101014", pane=4, window="1-ops", cmd=b"pwd")
    data, _ = export(p1 + ".raw.log", p2 + ".raw.log")
    check("two panes", len(data["panes"]) == 2, repr(data["panes"]))
    check("both logs in one page", {"id", "pwd"} <= set(cmds(data)), repr(cmds(data)))

    # 5. two sessions: pane ids can repeat, they stay apart, the page is not called after one of them
    a = session(os.path.join(T, "two"), "alpha", "20261101T101015", pane=3, server="1111", index=False)
    c = session(os.path.join(T, "two"), "beta", "20261102T101015", pane=3, server="2222", index=False)
    data, html = export(a + ".raw.log", c + ".raw.log")
    check("same pane id, two panes", len(data["panes"]) == 2, repr(data["panes"]))
    check("labels name the sessions", all(("alpha" in p["label"]) != ("beta" in p["label"]) for p in data["panes"]),
          repr(data["panes"]))
    check("several sessions: scope", data["meta"]["scope"] == "files" and data["meta"]["session"] == "logs",
          repr(data["meta"]))

    # 6. every file of one log given (a glob does that): one log, not three
    b6 = session(os.path.join(T, "same"), "work", "20261101T101016")
    open(b6 + ".log", "w", encoding="utf-8").write("clean\n")
    open(b6 + ".tsindex", "w", encoding="utf-8").write("1700000000 0\n")
    data, _ = export(b6 + ".raw.log", b6 + ".log", b6 + ".tsindex")
    check("same log once", cmds(data) == ["ls", "false"], repr(cmds(data)))
    data, _ = export(b6 + ".log")
    check(".log given, raw beside it: raw is read", cmds(data) == ["ls", "false"], repr(cmds(data)))

    # 7. raw gone: the cleaned .log, with a note
    d = os.path.join(T, "clean")
    os.makedirs(d)
    p = os.path.join(d, "tmux_work_0-zsh_p3_20261101T101017.log")
    open(p, "w", encoding="utf-8").write("user@host:~$ ls\nnotes.txt\nuser@host:~$ \n")
    data, _ = export(p)
    check("clean log: command", cmds(data) == ["ls"], repr(cmds(data)))
    check("clean log: note", any(".log から推定" in n for n in data["meta"]["notes"]), repr(data["meta"]["notes"]))

    # 8. split logs
    b8 = session(os.path.join(T, "parts"), "work", "20261101T101018")
    raw = open(b8 + ".raw.log", "rb").read()
    open(b8 + ".raw.log.part-aa", "wb").write(raw[:200])
    open(b8 + ".raw.log.part-ab", "wb").write(raw[200:])
    os.remove(b8 + ".raw.log")
    data, _ = export(b8 + ".raw.log.part-aa", b8 + ".raw.log.part-ab")
    check("parts joined", cmds(data) == ["ls", "false"], repr(cmds(data)))

    # 9. any file name: read as raw
    p = os.path.join(T, "copy-from-server.bin")
    shutil.copy(b1 + ".raw.log", p)
    data, _ = export(p)
    check("any name", cmds(data) == ["ls", "false"], repr(cmds(data)))

    # 10. the index of a copied log must not redirect the read to another file
    d = os.path.join(T, "trap")
    os.makedirs(os.path.join(d, ".rlindex"))
    secret = os.path.join(T, "secret")
    open(secret + ".raw.log", "wb").write(b"SECRET-TOKEN\r\n")
    p = os.path.join(d, "tmux_work_0-zsh_p3_20261101T101019")
    open(p + ".log", "w", encoding="utf-8").write("user@host:~$ ls\nnotes.txt\n")
    with open(os.path.join(d, ".rlindex", os.path.basename(p) + ".info"), "w", encoding="utf-8") as f:
        f.write("base=%s\nrawbase=%s\n" % (secret, secret))
    _, html = export(p + ".log")
    check("index cannot redirect the read", "SECRET-TOKEN" not in html)

    # 11. the sidecar next to the log is used: one interrupting line, no shell integration
    b = os.path.join(T, "chunks", "tmux_work_0-zsh_p3_20261101T101020")
    os.makedirs(os.path.dirname(b))
    z = Z("colon", nonce=None)
    z.prompt().accept(b"./serve.sh &").out(b"[1] 4321\r\n")
    z.prompt()
    offs = [len(z.s)]
    for ch in b"git st":
        z.type(bytes([ch]))
        offs.append(len(z.s))
    z.raw(b"[app] request handled\r\n")
    offs.append(len(z.s))
    for ch in b"atus":
        z.type(bytes([ch]))
        offs.append(len(z.s))
    z.accept(b"git status", typed=b"").out(b"On branch main\r\n")
    z.prompt()
    offs.append(len(z.s))
    open(b + ".raw.log", "wb").write(z.s)
    with open(b + ".tsindex", "w", encoding="utf-8") as f:
        f.write("".join("%d %d\n" % (1700000000 + i, o) for i, o in enumerate(offs)))
    data, _ = export(b + ".raw.log")
    check("sidecar: the typed command is whole", cmds(data)[-1:] == ["git status"], repr(cmds(data)))
    check("sidecar: command time", [x.get("t") for x in data["blocks"] if x["kind"] == "cmd"][-1] is not None)

    # 12. default output: next to the log, private, and no "latest" link that could replace a live session's
    r = emaki("--raw", b1 + ".raw.log")
    d = os.path.join(os.path.dirname(b1), "emaki")
    names = os.listdir(d) if os.path.isdir(d) else []
    check("default output dir", r.returncode == 0 and len(names) == 1 and names[0].startswith("emaki_work_"), repr(names))
    check("no latest link", not any("latest" in n for n in names), repr(names))
    if os.name == "posix":
        check("html is 600", os.stat(os.path.join(d, names[0])).st_mode & 0o777 == 0o600)
        check("dir is 700", os.stat(d).st_mode & 0o777 == 0o700)

    # 13. errors: one line, non zero
    r = emaki("--raw", os.path.join(T, "nothing.raw.log"))
    check("missing file", r.returncode == 1 and "見つかりません" in r.stderr.decode("utf-8", "replace"), repr(r.stderr))
    r = emaki("--raw", T)
    check("directory", r.returncode == 1 and "ディレクトリ" in r.stderr.decode("utf-8", "replace"), repr(r.stderr))
    r = emaki("--raw", b1 + ".raw.log", "-s", "work")
    check("--raw with -s", r.returncode == 2, repr(r.stderr))
    r = emaki()
    check("no tmux, no --raw: hint", r.returncode == 1 and "--raw" in r.stderr.decode("utf-8", "replace"), repr(r.stderr))
finally:
    shutil.rmtree(T, ignore_errors=True)

print("%d checks, %d failed" % (COUNT[0], len(FAILS)))
sys.exit(1 if FAILS else 0)
