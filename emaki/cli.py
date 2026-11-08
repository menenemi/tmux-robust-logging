"""emaki コマンド本体。

tmux の中で `emaki` と打つと、そのセッションのそれまでのログ（閉じたペインも含む）を
時系列順にまとめた 1 枚の HTML を書き出す。`emaki --raw <ログ>...` は、終了したセッションなどの
ログファイルから同じ HTML を書き出す（tmux は要らない）。
"""

import argparse
import getpass
import os
import subprocess
import sys
import time
import traceback

from . import __version__
from .collect import (LogFileError, TmuxError, capture_pane, collect, entries_from_files, file_component,
                      get_option, list_panes, load_clean, load_nonce_registry, load_raw, log_dirs,
                      nonces_for, session_info, tmux)
from .parse import TimeMap, lines_from_text, render_lines, split_raw
from .render import StyleTable
from .report import block_json, find_fira_code, meta, write_html
from .segment import build_blocks
from .timefmt import clean_log_parser

MODE_RANK = {"exact": 3, "readline": 2, "regex": 1, "capture": 0}


def _debug():
    return os.environ.get("EMAKI_DEBUG") == "1"


def _codec(name):
    if not name:
        return "utf-8"
    import codecs
    try:
        return codecs.lookup(name).name
    except LookupError:
        return "utf-8"


def _width(v):
    try:
        return int(v) if int(v) > 0 else None
    except (TypeError, ValueError):
        return None


def _pane_label(e):
    w = e.get("wname") or ""
    wi = e.get("widx") or "?"
    pi = e.get("pidx")
    return "%s:%s%s" % (wi, w, ("." + pi) if pi not in (None, "") else "")


def _with_session(label, session):
    # 窓・ペインを先に置く（HTML のペイン表示は幅で切られるので、長いセッション名が先だと
    # どの窓のコマンドか見分けられなくなる）
    return "%s · %s" % (label, session) if session else label


def _entry_lines(e, tbl, registry):
    """ログ 1 本を論理行にする → (lines, tmap, nonces, files, clean)。読めなければ None。"""
    rb = e["rawbase"] or e["base"]
    raw = load_raw(e)
    if raw is not None:
        tmap = TimeMap.load(rb + ".tsindex")
        nonces = nonces_for(e, registry)
        lines = render_lines(split_raw(raw, nonces, tmap), tbl, _codec(e.get("conv")), tmap,
                             width=e.get("width"))
        e["unverified"] = not nonces and b"\x1b]133;" in raw
        return lines, tmap, nonces, [e.get("rawfile") or rb + ".raw.log"], False
    text = load_clean(e)
    if text is None:
        return None
    # raw が残っていない: 整形済み .log から（シェル統合の印も時刻 sidecar も無い）
    lines = lines_from_text(text, tbl, clean_log_parser(text, e.get("tsfmt"), e.get("start")))
    return lines, None, None, [e["base"] + ".log"], True


def _files_session(entries):
    """--raw: ログの持ち主を「セッション」の形にまとめる（複数のセッションのログなら名前は logs）。"""
    names = sorted({e["session"] for e in entries if e["session"]})
    hosts = sorted({e["host"] for e in entries if e["host"]})
    return {"pid": "", "id": "", "name": names[0] if len(names) == 1 else "logs", "created": 0,
            "host": hosts[0] if len(hosts) == 1 else "", "server_start": 0, "several": len(names) > 1}


def export(args):
    notes = []
    if args.raw:
        entries = entries_from_files(args.raw)
        sess = _files_session(entries)
        panes = []
        out_base = os.path.dirname(entries[0]["base"])
        # 書き出し先は同じサーバとは限らないので、台帳は全部（ログごとにサーバ PID とペインで絞る）
        registry = load_nonce_registry()
    else:
        if args.from_tmux:
            target = args.from_tmux
        elif args.session:
            target = "=" + args.session + ":"
        elif os.environ.get("TMUX_PANE"):
            target = os.environ["TMUX_PANE"]
        elif os.environ.get("TMUX"):
            target = None
        else:
            raise TmuxError("tmux の中で実行するか、-s でセッション名を指定してください"
                            "（ログファイルから書き出すなら emaki --raw <ログ>...）")

        sess = session_info(target)
        dirs, out_base, raw_only = log_dirs()
        panes = list_panes(sess, args.all)
        entries = collect(sess, dirs, panes, args.all, raw_only, notes)
        # シェル統合の nonce 台帳（このサーバの分だけ残す。台帳は全サーバ・全期間の追記なので）
        registry = []
        if entries:
            registry = [r for r in load_nonce_registry() if r["server_pid"] == sess["pid"]]

    tbl = StyleTable()
    pane_rows = []   # 表示用のペイン一覧
    pane_index = {}  # pane_id -> index
    blocks = []      # (sortkey, block_json)
    seq = 0
    logged = set()   # ログを書き出せたペイン（スクロールバックでは補わない）

    def pane_of(key, pid, e, live, mode, files):
        i = pane_index.get(key)
        if i is None:
            i = len(pane_rows)
            pane_index[key] = i
            pane_rows.append({"id": pid or "", "label": "", "window": "", "session": "",
                              "live": live, "mode": mode, "files": [], "color": i % 8})
        p = pane_rows[i]
        if not p["label"] or e.get("live"):  # 窓名などは今ロギング中のログ（今の値）を優先
            p["label"], p["window"] = _pane_label(e), e.get("wname", "")
            p["session"] = e.get("session") or ""
        p["live"] = p["live"] or live
        if MODE_RANK.get(mode, 0) > MODE_RANK.get(p["mode"], 0):
            p["mode"] = mode
        for f in files:
            if f not in p["files"]:
                p["files"].append(f)
        return i

    def label_of(e):
        lb = _pane_label(e)
        return _with_session(lb, e.get("session")) if args.all else lb

    # ログ 1 本・ペイン 1 つが壊れていても（想定外の例外でも）、注記を残して残りは書き出す。
    # ブロックとペイン行は、そのログを最後まで処理できてから登録する（中途半端に残さない）。
    live_ids = {p["pane_id"] for p in panes}
    for e in entries:
        key = e.get("pane") or ("?" + e["base"])
        if args.raw:  # 別のサーバ・セッションのログでは、ペイン ID が重なることがある
            key = (e.get("server_pid"), e.get("session"), key)
        try:
            got = _entry_lines(e, tbl, registry)
            if got is None:
                if args.raw:
                    notes.append("%s を読めませんでした" % os.path.basename(e["base"]))
                continue
            lines, tmap, nonces, files, clean = got
            bl, mode = build_blocks(lines, tmap, nonces)
            del lines, got
            pi = pane_index.get(key, len(pane_rows))
            start = e.get("start")
            out = []
            for b in bl:
                seq += 1
                t = b["t"] if b["t"] is not None else start
                out.append(((t if t is not None else float("inf"), start or 0, seq),
                            block_json(b, pi, args.max_lines)))
        except Exception as ex:
            notes.append("ペイン %s のログ %s を処理できませんでした（%s）"
                         % (label_of(e), os.path.basename(e["base"]), type(ex).__name__))
            if _debug():
                traceback.print_exc()
            continue
        if clean:
            notes.append("raw が残っていないログは整形済み .log から推定しています（色・終了コードなし）")
        if args.raw and e.get("unverified"):
            notes.append("シェル統合の印を確かめられないログは、入力行から推定しています"
                         "（索引か nonce の台帳が無いログ。終了コードなし）")
        if e.get("pane"):
            logged.add(e["pane"])
        pane_of(key, e.get("pane"), e, e.get("pane") in live_ids, mode, files)
        blocks.extend(out)

    # ログが 1 本も無いペインはスクロールバックで補う。ロギングを途中で止めたペインは補わない
    # （スクロールバックにはログと同じ内容が入っていて、全コマンドが二重になるため）
    if not args.no_capture:
        for pn in panes:
            if pn["pane_id"] in logged:
                continue
            e = {"base": pn["pane_id"], "wname": pn["window_name"], "widx": pn["window_index"],
                 "pidx": pn["pane_index"], "session": pn["session_name"]}
            try:
                data = capture_pane(pn["pane_id"])
                if not data or not data.strip():
                    continue
                lines = render_lines(split_raw(data), tbl, width=_width(pn["pane_width"]))
                bl, _ = build_blocks(lines, None, None)
                del lines
                pi = pane_index.get(pn["pane_id"], len(pane_rows))
                out = []
                for b in bl:
                    seq += 1
                    out.append(((float("inf"), float("inf"), seq),
                                block_json(b, pi, args.max_lines)))
            except Exception as ex:
                notes.append("ペイン %s のスクロールバックを処理できませんでした（%s）"
                             % (label_of(e), type(ex).__name__))
                if _debug():
                    traceback.print_exc()
                continue
            pane_of(pn["pane_id"], pn["pane_id"], e, True, "capture", [])
            blocks.extend(out)

    # 複数のセッションが混ざる（--all など）ときは、ラベルにセッション名を付ける
    if args.all or len({p["session"] for p in pane_rows}) > 1:
        for p in pane_rows:
            p["label"] = _with_session(p["label"], p["session"])

    # ロギングを止めた（prefix + P など）ペインは、止めた後の出力をスクロールバックから補わない
    # （ログと同じ内容が二重になる。止めたのは記録したくない作業のためかもしれない）ので、そう注記する
    for pn in panes:
        i = pane_index.get(pn["pane_id"])
        if pn["pane_id"] in logged and not pn["@rl_base"] and i is not None:
            notes.append("ペイン %s はロギングが止まっているため、止めた後の出力は含まれません"
                         % pane_rows[i]["label"])

    blocks.sort(key=lambda x: x[0])
    extra = {
        "user": getpass.getuser() if hasattr(getpass, "getuser") else "",
        "scope": "server" if args.all else "files" if sess.get("several") else "session",
        "notes": sorted(set(notes)),
    }
    data = {
        "meta": meta(sess, extra),
        "styles": tbl.to_json(),
        "panes": pane_rows,
        "blocks": [b for _, b in blocks],
    }

    stem = file_component(sess["name"])  # 文字の途中で切らない（壊れたファイル名にしない）
    if args.output:
        out = os.path.abspath(os.path.expanduser(args.output))
    else:
        hd = os.path.expanduser(get_option("@rl_emaki_dir", "")) or os.path.join(out_base, "emaki")
        os.makedirs(hd, exist_ok=True)
        try:
            os.chmod(hd, 0o700)
        except OSError:
            pass
        out = os.path.join(hd, "emaki_%s_%s.html" % (stem, time.strftime("%Y%m%d-%H%M%S")))
    title = "emaki · %s — %s" % ("tmux server" if args.all else "logs" if sess.get("several")
                                 else "session " + sess["name"], sess.get("host", ""))
    embed = args.embed_font or get_option("@rl_emaki_embed_font", "off") == "on"
    size = write_html(data, out, title, embed_font=embed)

    if not args.output and not args.raw:
        latest = os.path.join(os.path.dirname(out), "emaki_%s_latest.html" % stem)
        try:  # 最新版へのリンク（何度出し直しても同じパスで開ける）
            tmp = latest + ".tmp"
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(os.path.basename(out), tmp)
            os.replace(tmp, latest)
        except OSError:
            pass

    ncmd = sum(1 for _, b in blocks if b["kind"] == "cmd") + \
        sum(len(b.get("children", [])) for _, b in blocks)
    stats = {"commands": ncmd, "panes": len(pane_rows), "bytes": size,
             "modes": sorted({p["mode"] for p in pane_rows}), "notes": data["meta"]["notes"]}
    return out, stats


def _open(path):
    for cmd in (["xdg-open", path], ["open", path]):
        try:
            # stdin も切る（run-shell のジョブの入出力を握ったままブラウザが居座らないように）
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError:
            continue
    return False


def _robust_stdio():
    # 表示できない文字（壊れたファイル名など）で print が落ちないように
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            pass


def _short(path):
    home = os.path.expanduser("~").rstrip("/\\")
    if home and home != "~" and path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _display(msg):
    """tmux のステータス行に 1 行だけ出す（run-shell -b の中では、標準出力に書くと
    アクティブなペインが view mode に切り替わってしまうため、こちらで知らせる）。"""
    msg = " ".join(str(msg).split())
    if len(msg) > 200:
        msg = msg[:199] + "…"
    try:
        tmux("display-message", msg.replace("#", "##"))  # メッセージは format 展開される
    except TmuxError:
        pass


def _error(args, msg, rc):
    """失敗を 1 行で知らせて、終了コードを返す。"""
    if not args.from_tmux:
        sys.stderr.write(msg + "\n")
        return rc
    # キーバインド（run-shell -b）から: 0 以外で終わると tmux が "'…' returned 1" を
    # アクティブなペインの view mode に出してしまう（ssh 中のペインなどでキー入力を奪う）ので、
    # ステータス行で知らせたうえで 0 で終わる
    _display(msg)
    return 0


def main(argv=None):
    if sys.version_info < (3, 7):
        sys.stderr.write("emaki: Python 3.7 以上が必要です\n")
        return 2
    _robust_stdio()
    ap = argparse.ArgumentParser(
        prog="emaki",
        description="tmux セッションのログ（tmux-robust-logging が記録したもの）を、"
                    "コマンド単位に折りたためる 1 枚の HTML にまとめます。")
    ap.add_argument("-s", "--session", help="対象のセッション名（省略時: このコマンドを打ったセッション）")
    ap.add_argument("-o", "--output", help="出力先の HTML（省略時: <ログ保存先>/emaki/ に自動で命名）")
    ap.add_argument("--open", action="store_true", help="生成後にブラウザで開く")
    ap.add_argument("--all", action="store_true", help="tmux サーバ上の全セッションを 1 枚にまとめる")
    ap.add_argument("--raw", nargs="+", metavar="ログ",
                    help="指定したログファイルから書き出す（終了したセッションのログ用。tmux は要らない）。"
                         "*.raw.log のほか、raw が残っていなければ *.log も読める。複数指定すると時系列で 1 枚に")
    ap.add_argument("--no-capture", action="store_true",
                    help="ログの無いペインのスクロールバックを含めない")
    ap.add_argument("--max-lines", type=int, default=20000,
                    help="1 コマンドの出力の最大行数。超えた分は中略（既定 20000、0 で無制限）")
    ap.add_argument("--embed-font", action="store_true",
                    help="Fira Code フォントを HTML に埋め込む（別の PC で開いても同じ見た目になる。"
                         "数百 KB 増える）")
    ap.add_argument("--from-tmux", metavar="PANE", help=argparse.SUPPRESS)  # キーバインド用
    ap.add_argument("-q", "--quiet", action="store_true", help="結果のパス以外を表示しない")
    ap.add_argument("-V", "--version", action="version", version="emaki " + __version__)
    args = ap.parse_args(argv)
    if args.raw and (args.session or args.all or args.from_tmux):
        ap.error("--raw は -s / --all と一緒には使えません")

    try:
        path, st = export(args)
    except KeyboardInterrupt:
        return 0 if args.from_tmux else 130
    except (TmuxError, LogFileError) as ex:
        return _error(args, "emaki: %s" % ex, 1)
    except Exception as ex:  # 想定外でも 1 行で知らせる（詳細は EMAKI_DEBUG=1 で）
        rc = _error(args, "emaki: 書き出しに失敗しました（%s: %s）" % (type(ex).__name__, ex), 1)
        if _debug():
            traceback.print_exc()
        return rc

    opened = _open(path) if args.open else True
    if args.from_tmux:  # キーバインドから: 標準出力・標準エラーには何も出さない
        head = "emaki: %d コマンド%s%s → " % (
            st["commands"], "（注記 %d 件）" % len(st["notes"]) if st["notes"] else "",
            "" if opened else "（ブラウザを開けませんでした）")
        sp = _short(path)
        if len(head) + len(sp) > 200:  # 長いパスは頭を削る（ファイル名を残す）
            sp = "…" + sp[len(head) + len(sp) - 199:]
        _display(head + sp)
        return 0
    if args.quiet:
        print(path)
    else:
        print("emaki: %d コマンド / %d ペイン → %s (%.1f KB)"
              % (st["commands"], st["panes"], path, st["bytes"] / 1024.0))
        for n in st["notes"]:  # HTML の先頭にも出る
            print("  注記: " + n)
        if st["modes"] and set(st["modes"]) <= {"readline", "regex", "capture"} and not args.raw:
            print("  ヒント: シェル統合を入れると、コマンドの区切りと終了コードが正確になります"
                  "（README「emaki」参照）")
        try:
            have_font = bool(find_fira_code())
        except Exception:
            have_font = True
        if not have_font:
            print("  ヒント: Fira Code が見つかりません（sudo apt install fonts-firacode で入ります）。"
                  "代替フォントで表示されます")
    if not opened:
        sys.stderr.write("emaki: ブラウザを開けませんでした: %s\n" % path)
    return 0
