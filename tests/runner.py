"""Run every scenario of a scenario file through ONE plugin dir and dump a comparable summary.

usage: python runner.py <plugin dir> <scenarios.json> <out.json>
Each scenario: {"name", "pipe": raw|capture|clean, "data": base64 bytes (raw/capture) or text (clean),
                "width": int|None, "reg": {nonce: {...}} (raw only; default: lib_audit's REG)}
The plugin under test is imported from <plugin dir> only.
"""
import base64
import json
import os
import sys

PLUGIN = sys.argv[1]
sys.path.insert(0, PLUGIN)
from emaki.parse import TimeMap, split_raw, render_lines, lines_from_text  # noqa: E402
from emaki.render import StyleTable  # noqa: E402
from emaki.report import block_json  # noqa: E402
from emaki.segment import build_blocks  # noqa: E402
from emaki.timefmt import clean_log_parser  # noqa: E402
import emaki.segment  # noqa: E402

assert os.path.abspath(emaki.segment.__file__).startswith(os.path.abspath(PLUGIN)), emaki.segment.__file__

N1 = "0123456789abcdef"
N2 = "fedcba9876543210"
REG = {N1: {"order": 0}, N2: {"order": 1, "parent": N1}}


def row_text(r):
    if isinstance(r, str):
        return r
    if isinstance(r, list):
        return "".join(x[0] for x in r)
    return r


def runs_text(txt, runs, tbl):
    """[[len, sid]] -> [[text, fg, flags]] (UTF-16 lengths), for a readable pr/cr comparison"""
    out = []
    i = 0
    for n, sid in runs:
        k = 0
        s = ""
        while k < n and i < len(txt):
            s += txt[i]
            k += 2 if ord(txt[i]) > 0xFFFF else 1
            i += 1
        st = tbl.list[sid]
        out.append([s, st[0] or "-", st[2]])
    return out


def summ(d, tbl):
    e = {"kind": d["kind"], "cmd": d.get("cmd"), "prompt": d.get("prompt"), "exit": d.get("exit"),
         "xi": bool(d.get("xi")), "out": [row_text(r) for r in d.get("out", [])]}
    for k in ("remote", "label", "cwd", "running", "exact", "screen"):
        if d.get(k):
            e[k] = d[k]
    if "pr" in d:
        e["pr"] = runs_text(d.get("prompt") or "", d["pr"], tbl)
    if "cr" in d:
        e["cr"] = runs_text(d.get("cmd") or "", d["cr"], tbl)
    if d.get("children"):
        e["children"] = [summ(c, tbl) for c in d["children"]]
    return e


def run(sc):
    tbl = StyleTable()
    w = sc.get("width")
    if sc["pipe"] == "raw":
        raw = base64.b64decode(sc["data"])
        reg = sc.get("reg", REG)
        tm = TimeMap([tuple(x) for x in sc["ts"]]) if sc.get("ts") else None
        lines = render_lines(split_raw(raw, reg, tm), tbl, "utf-8", tm, width=w)
        bl, mode = build_blocks(lines, tm, reg)
    elif sc["pipe"] == "capture":
        data = base64.b64decode(sc["data"])
        lines = render_lines(split_raw(data), tbl, width=w)
        bl, mode = build_blocks(lines, None, None)
    else:
        text = sc["data"]
        lines = lines_from_text(text, tbl, clean_log_parser(text, None, None))
        bl, mode = build_blocks(lines, None, None)
    js = [block_json(b, 0, 20000) for b in bl]
    return {"mode": mode, "blocks": [summ(d, tbl) for d in js]}


def main():
    scs = json.load(open(sys.argv[2], encoding="utf-8"))
    res = {}
    for sc in scs:
        try:
            res[sc["name"]] = run(sc)
        except Exception:  # noqa
            import traceback
            res[sc["name"]] = {"error": traceback.format_exc()}
    json.dump(res, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    print(len(res), "scenarios ->", sys.argv[3])


main()
