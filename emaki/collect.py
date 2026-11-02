"""tmux とファイルシステムから、対象セッションのログを集める。

ログの見つけ方（同じログは 1 回だけ）:
  1. 索引 <ログ保存先>/.rlindex/*.info と <@rl_raw_dir>/.rlindex/*.info（start_logging.sh が記録。
     どのサーバ・セッション・ペインのログかが確実に分かる）
  2. 今ロギング中のペイン（pane option の @rl_base など）
  3. 索引の無い古いログ: ファイル名 tmux_<セッション>_<窓番号>-<窓名>_p<ペイン>_<開始時刻> から推定
     （セッション名が一致し、開始時刻がセッション作成以降のもの）。索引のあるログは、対象外として
     はじいたもの（別サーバ・別セッション）や中身の壊れたものも含めてここでは拾わない。
     @rl_raw_dir にしか無いものは、索引付きで記録するようになった後の分を拾わない（索引が
     別のログ保存先にある、@rl_raw_dir を共有する別サーバのログの可能性があるため）
ログが 1 本も無いペインは、tmux のスクロールバック（capture-pane）で補う。

シェル統合の印が本物かどうかは、シェルが起動時に登録する nonce の台帳（nonce_registry_path）で
確かめる（コマンドの出力が印を偽造して、コマンドや終了コードを作れないように）。
"""

import glob
import os
import re
import shlex
import subprocess
import time

US = "\x1f"
_NAME_RE = re.compile(r"^tmux_(?P<rest>.*)_p(?P<pane>\d+)_(?P<ts>\d{8}T\d{6})(?:_\d+)?$")
_BAD = set('/\\:*?"<>|\'`$&;() ')
_SUFFIXES = (".raw.log", ".tsindex", ".log")
_WIN_RE = re.compile(r"_(\d+)-")
_NONCE_RE = re.compile(r"[0-9a-f]{8,32}\Z")
_PANE_RE = re.compile(r"%\d{1,9}\Z")
_NONCE_MAX = 5 << 20  # 台帳は追記のみで育つので、読むのは末尾の 5 MB まで


class TmuxError(Exception):
    pass


class LogFileError(Exception):
    """--raw で指定されたログを読めない。"""


def _tmux_cmd():
    # EMAKI_TMUX で tmux の呼び方を変えられる（別ソケット: EMAKI_TMUX="tmux -L work" など）
    return shlex.split(os.environ.get("EMAKI_TMUX") or "tmux")


def tmux(*args, raw=False, check=False):
    try:
        p = subprocess.run(_tmux_cmd() + [str(a) for a in args],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        raise TmuxError("tmux コマンドが見つかりません")
    if p.returncode != 0:
        if check:
            msg = p.stderr.decode("utf-8", "replace").strip()
            raise TmuxError(msg or "tmux の実行に失敗しました")
        return None
    if raw:
        return p.stdout
    return p.stdout.decode("utf-8", "surrogateescape").rstrip("\n")


def get_option(name, default=""):
    try:
        v = tmux("show-option", "-gqv", name)
    except TmuxError:  # tmux が無い（emaki --raw は tmux なしで動く）
        v = None
    return v if v else default


def _expand(p):
    return os.path.expanduser(p) if p else p


def _sanitize_bytes(name):
    b = name.encode("utf-8", "surrogateescape")
    return bytes(0x5F if ch < 0x20 or ch == 0x7F or (ch < 0x80 and chr(ch) in _BAD) else ch
                 for ch in b)


def sanitize_component(name):
    """helpers.sh の rl_sanitize_component と同じ変換（バイト単位、60 バイトで切る）。

    既存のログのファイル名との照合用（バイト単位で一致させるため、文字の途中で切れることがある）。
    emaki が新しく作るファイルの名前には file_component を使う。
    """
    out = _sanitize_bytes(name)[:60]
    return out.decode("utf-8", "surrogateescape") if out else "x"


def file_component(name):
    """出力ファイル名用。sanitize_component と同じ変換だが、60 バイトを UTF-8 の文字境界で切る
    （文字の途中で切ると壊れたファイル名になり、パスの表示で落ちる）。"""
    return _sanitize_bytes(name)[:60].decode("utf-8", "ignore") or "x"


def _same(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return a == b


def log_dirs():
    """(探すディレクトリ一覧, HTML の既定の保存先の親, raw だけを置くディレクトリ一覧) を返す。
    helpers.sh の rl_log_dir / rl_raw_dir と同じ規則。"""
    uid = os.getuid() if hasattr(os, "getuid") else 0
    tmp = os.environ.get("TMPDIR") or "/tmp"
    main = _expand(get_option("@rl_log_dir", "~/tmux-logs"))
    rawd = _expand(get_option("@rl_raw_dir", ""))
    fb = os.path.join(tmp, "tmux-logs-%d" % uid)
    search = []
    for d in (main, rawd, fb):
        if d and os.path.isdir(d) and not any(_same(d, x) for x in search):
            search.append(d)
    out_base = main if (main and os.path.isdir(main) and os.access(main, os.W_OK)) else fb
    # @rl_raw_dir（整形ログの置き場所と別のとき）。複数のサーバで共有されることがある
    raw_only = [d for d in search if _same(d, rawd) and not _same(d, main) and not _same(d, fb)] \
        if rawd else []
    return search, out_base, raw_only


def session_info(target):
    fmt = US.join(["#{pid}", "#{session_id}", "#{session_name}", "#{session_created}", "#{host}",
                   "#{start_time}"])
    args = ["display-message", "-p"]
    if target:
        args += ["-t", target]
    out = tmux(*(args + [fmt]), check=True)
    parts = out.split(US)
    if len(parts) < 4:
        raise TmuxError("tmux からセッション情報を取得できませんでした")
    parts += [""] * (6 - len(parts))
    pid, sid, name, created, host, started = parts[:6]
    return {"pid": pid, "id": sid, "name": name, "created": _int(created),
            "host": host, "server_start": _int(started)}


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_PANE_KEYS = ["pane_id", "session_name", "window_index", "window_name", "pane_index",
              "pane_current_path", "@rl_base", "@rl_rawbase", "@rl_parts", "@rl_conv",
              "@rl_tsfmt", "pane_width", "session_id"]


def list_panes(sess, all_server=False):
    fmt = US.join("#{%s}" % k for k in _PANE_KEYS)
    args = ["list-panes", "-a"] if all_server else ["list-panes", "-s", "-t", sess["id"]]
    out = tmux(*(args + ["-F", fmt]), check=True)
    panes = []
    for ln in out.split("\n"):
        if not ln:
            continue
        vals = ln.split(US)
        vals += [""] * (len(_PANE_KEYS) - len(vals))
        panes.append(dict(zip(_PANE_KEYS, vals)))
    for p in panes:
        # 古い tmux でフォーマットからペインオプションが引けない場合の保険
        if not p["@rl_base"]:
            v = tmux("show-options", "-pqv", "-t", p["pane_id"], "@rl_base")
            if v:
                p["@rl_base"] = v
                for k in ("@rl_rawbase", "@rl_parts", "@rl_conv", "@rl_tsfmt"):
                    p[k] = tmux("show-options", "-pqv", "-t", p["pane_id"], k) or ""
    return panes


def read_kv(path):
    d = {}
    try:
        with open(path, "rb") as f:
            for ln in f:
                k, sep, v = ln.rstrip(b"\r\n").partition(b"=")
                if sep:
                    d[k.decode("ascii", "replace")] = v.decode("utf-8", "surrogateescape")
    except OSError:
        return None
    return d


def _base_name(fn):
    if fn.endswith((".log.tmp", ".history.txt", ".screen.txt")):
        return None
    if ".raw.log.part-" in fn:
        return fn.split(".raw.log.part-", 1)[0]
    for suf in _SUFFIXES:
        if fn.endswith(suf):
            return fn[:-len(suf)]
    return None


def _ts_epoch(ts):
    try:
        return time.mktime(time.strptime(ts, "%Y%m%dT%H%M%S"))
    except (ValueError, OverflowError):
        return None


def _none(v):
    return None if v in (None, "", "none") else v


def _tsfmt(v):
    # "none"（日時なしで記録した）と未記録（None = 不明）は区別する。前者は行頭を一切読まない
    return None if v in (None, "") else v


def _start_of(base):
    m = _NAME_RE.match(os.path.basename(base))
    return _ts_epoch(m.group("ts")) if m else None


def _split_rest(rest, names):
    """ファイル名の "<セッション>_<窓番号>-<窓名>" を (セッション, 窓番号, 窓名) に分ける。

    names: {ファイル名用に変換したセッション名: 元の名前}。一致するものがあればそれを使い、
    無ければ最後の "_<数字>-" で分ける。分けられなければ None。
    """
    for k in sorted(names, key=len, reverse=True):
        if rest.startswith(k + "_"):
            m = re.match(r"(\d+)-(.*)\Z", rest[len(k) + 1:], re.S)
            if m:
                return names[k], m.group(1), m.group(2)
    last = None
    for last in _WIN_RE.finditer(rest):
        pass
    if last is None:
        return None
    return rest[:last.start()], last.group(1), rest[last.end():]


def collect(sess, dirs, panes, all_server=False, raw_only=(), notes=None):
    """対象のログを集めて [entry] を返す（開始時刻順）。

    raw_only: raw だけを置くディレクトリ（log_dirs() の 3 つ目）。
    notes: リストを渡すと、持ち主を確かめられずにはじいたログの注記（本数だけ）を足す。
    """
    entries = {}     # ファイル名の base -> entry
    indexed = set()  # 索引のある base 名（対象外としてはじいたもの・中身の壊れたものも含む）
    decided = set()  # 索引の中身で、対象かどうかを判定できた base 名
    unsure = {}      # 索引が壊れていて判定できなかった base 名 -> 開始時刻
    oldest = None    # 索引のある最古のログの開始時刻（索引付きで記録するようになった時期の目安）
    # 今のセッション名（索引の名前は記録時のものなので、改名されていれば今の名前で出す）
    cur_names = {pn.get("session_id"): pn["session_name"] for pn in panes if pn.get("session_id")}
    cur_names.setdefault(sess["id"], sess["name"])

    def add(e):
        key = os.path.basename(e["base"])
        old = entries.get(key)
        if old is None:
            entries[key] = e
            return e
        for k, v in e.items():  # 情報の多い方を残す
            if old.get(k) in (None, "") and v not in (None, ""):
                old[k] = v
        return old

    # 1. 索引
    for d in dirs:
        for p in glob.glob(os.path.join(glob.escape(d), ".rlindex", "*.info")):
            stem = os.path.basename(p)[:-len(".info")]
            # 索引ファイルがあるログは、中身が壊れていても（空・書きかけ）手順 3 でファイル名から
            # 拾わない。持ち主（サーバ・セッション）を中身で確かめられたものだけを入れる
            indexed.add(stem)
            info = read_kv(p) or {}
            # base= が無ければ置き場所から（索引は <base のディレクトリ>/.rlindex/<base 名>.info）
            base = info.get("base") or os.path.join(os.path.dirname(os.path.dirname(p)), stem)
            indexed.add(os.path.basename(base))
            start = _int(info.get("start"), None)
            if start is None:
                start = _start_of(base)
            if start is not None and (oldest is None or start < oldest):
                oldest = start
            spid = info.get("server_pid")
            if spid and spid != sess["pid"]:
                decided.add(stem)
                continue  # 別サーバ
            if not spid or not (all_server or (info.get("session_id")
                                               and info.get("session_created"))):
                unsure[stem] = start  # 判定に要る項目が無い
                continue
            decided.add(stem)
            if all_server:
                # PID はサーバの再起動（OS の再起動）で使い回されることがあるので、開始時刻でも確かめる
                if sess["server_start"] and (start is None or start < sess["server_start"] - 2):
                    continue
            elif (info.get("session_id") != sess["id"]
                  or _int(info.get("session_created")) != sess["created"]):
                continue
            add({"base": base, "rawbase": info.get("rawbase") or base,
                 "parts": info.get("parts") == "1", "conv": _none(info.get("conv")),
                 "tsfmt": _tsfmt(info.get("tsfmt")), "pane": info.get("pane_id"),
                 "server_pid": info.get("server_pid"),
                 "session": cur_names.get(info.get("session_id")) or info.get("session_name", ""),
                 "widx": info.get("window_index", ""), "wname": info.get("window_name", ""),
                 "pidx": info.get("pane_index", ""), "start": start,
                 "user": info.get("user", ""), "host": info.get("host", ""),
                 "width": _int(info.get("pane_width"), None) or None, "live": False})

    # 2. 今ロギング中のペイン
    for pn in panes:
        if not pn["@rl_base"]:
            continue
        e = add({"base": pn["@rl_base"], "rawbase": pn["@rl_rawbase"] or pn["@rl_base"],
                 "parts": pn["@rl_parts"] == "1", "conv": _none(pn["@rl_conv"]),
                 "tsfmt": _tsfmt(pn["@rl_tsfmt"]), "pane": pn["pane_id"],
                 "server_pid": sess["pid"], "session": pn["session_name"],
                 "widx": pn["window_index"], "wname": pn["window_name"],
                 "pidx": pn["pane_index"], "start": None, "user": "", "host": "",
                 "width": _int(pn["pane_width"], None) or None, "live": True})
        e["live"] = True
        # 窓名・端末幅などは今の値を優先
        e["wname"], e["widx"], e["pidx"] = pn["window_name"], pn["window_index"], pn["pane_index"]
        e["session"] = pn["session_name"] or e.get("session", "")
        e["width"] = _int(pn["pane_width"], None) or e.get("width")

    # 3. 索引の無い古いログ（ファイル名から）
    sname = sanitize_component(sess["name"])  # 照合はバイト単位で（ファイル名と同じ切り方）
    own = re.compile(re.escape("tmux_" + sname + "_") + r"\d+-")  # セッション名 + "_" + 窓番号 + "-"
    known = {sname: sess["name"]}
    if all_server:
        for pn in panes:
            known.setdefault(sanitize_component(pn["session_name"]), pn["session_name"])
    since = (sess["server_start"] or 0) if all_server else sess["created"]

    def by_name(bn, st):  # 索引が無いとしたら、ファイル名の規則でこの書き出しに入るログか
        return st is not None and st >= since - 2 and (all_server or bool(own.match(bn)))

    found = {}
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        ro = any(_same(d, x) for x in raw_only)
        for fn in names:
            if not fn.startswith("tmux_"):
                continue
            bn = _base_name(fn)
            if bn is None or bn in indexed:
                continue
            m = _NAME_RE.match(bn)
            if not m:
                continue
            st = _ts_epoch(m.group("ts"))
            if not by_name(bn, st):
                continue
            slot = found.setdefault(bn, {"start": st, "pane": "%" + m.group("pane"),
                                         "rest": m.group("rest"), "base": None, "rawbase": None,
                                         "raw_only": True})
            slot["raw_only"] = slot["raw_only"] and ro
            if fn == bn + ".log":
                slot["base"] = os.path.join(d, bn)
            else:
                slot["rawbase"] = os.path.join(d, bn)
    foreign = 0
    for bn, s in found.items():
        if bn in entries:
            if entries[bn].get("start") is None:
                entries[bn]["start"] = s["start"]
            continue
        # @rl_raw_dir にしか無く（整形ログの置き場所に何も無い）、索引付きで記録するように
        # なった後に始まったログ: 自分のサーバのログなら索引があるはず。@rl_raw_dir を共有する
        # 別サーバ（索引は別のログ保存先にある）のログかもしれないので入れない
        if s["raw_only"] and oldest is not None and s["start"] >= oldest - 2:
            foreign += 1
            continue
        base = s["base"] or s["rawbase"]
        if all_server:
            sp = _split_rest(s["rest"], known) or ("", "", "")
        else:
            sp = _split_rest(s["rest"], {sname: sess["name"]}) or (sess["name"], "", "")
        add({"base": base, "rawbase": s["rawbase"] or base, "parts": False, "conv": None,
             "tsfmt": None, "pane": s["pane"], "server_pid": None, "session": sp[0],
             "widx": sp[1], "wname": sp[2], "pidx": "", "start": s["start"], "user": "",
             "host": "", "width": None, "live": False})

    if notes is not None:
        # はじいたことは書き出す側にも分かるように（ファイル名・中身は書かない）
        broken = sum(1 for bn, st in unsure.items()
                     if bn not in decided and bn not in entries and by_name(bn, st))
        if broken:
            notes.append("索引（.rlindex）が壊れていて、どのセッションのものか確かめられないログ "
                         "%d 本は含めていません" % broken)
        if foreign:
            notes.append("索引の無いログ %d 本は、@rl_raw_dir を共有する別の tmux サーバのもの"
                         "かもしれないため含めていません" % foreign)

    out = list(entries.values())
    for e in out:
        if e.get("start") is None:
            e["start"] = _start_of(e["base"])
    out.sort(key=lambda e: (e["start"] if e["start"] is not None else float("inf"), e["base"]))
    return out


_LOG_SUFFIX = re.compile(r"(?:\.raw\.log(?:\.part-[^/\\]*)?|\.tsindex|\.log)\Z")


def entries_from_files(paths):
    """指定されたログファイル → [entry]（開始時刻順）。

    終了したセッションのログ・別の PC から持ってきたログ用なので、tmux にもログ保存先にも問い合わせない。
    同じログの .raw.log / .log / .tsindex を重ねて指定しても 1 本にまとめる。隣に索引（.rlindex）があれば窓名などを
    読むが、読むのは指定されたログだけ（索引の rawbase= は、同じ名前の raw を指すときしか使わない）。
    名前の規則に合わないファイルは raw としてそのまま読む。
    """
    entries = {}
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p):
            raise LogFileError("%s はディレクトリです。ログのファイルを指定してください（例: %s）"
                               % (p, os.path.join(p, "tmux_<セッション>_*.raw.log")))
        if not os.path.isfile(p):
            raise LogFileError("ファイルが見つかりません: %s" % p)
        m = _LOG_SUFFIX.search(p)
        base = p[:m.start()] if m else p
        if base in entries:
            continue
        d, name = os.path.split(base)
        info = (read_kv(os.path.join(d, ".rlindex", name + ".info")) or {}) if m else {}
        rawbase = base
        rb = info.get("rawbase")
        if rb and os.path.basename(rb) == name and not os.path.isfile(base + ".raw.log") \
                and os.path.isfile(rb + ".raw.log"):
            rawbase = rb
        fm = _NAME_RE.match(name) if m else None
        sp = (_split_rest(fm.group("rest"), {}) if fm else None) or ("", "", "")
        start = _int(info.get("start"), None)
        e = {"base": base, "rawbase": rawbase, "parts": False, "conv": _none(info.get("conv")),
             "tsfmt": _tsfmt(info.get("tsfmt")), "pane": info.get("pane_id") or ("%" + fm.group("pane") if fm else None),
             "server_pid": info.get("server_pid"), "session": info.get("session_name") or sp[0],
             "widx": info.get("window_index") or sp[1], "wname": info.get("window_name") or sp[2] or name,
             "pidx": info.get("pane_index", ""), "start": start if start is not None else _start_of(base),
             "user": info.get("user", ""), "host": info.get("host", ""),
             "width": _int(info.get("pane_width"), None) or None, "live": False}
        if not m:
            e["rawfile"] = p
        entries[base] = e
    out = list(entries.values())
    out.sort(key=lambda e: (e["start"] if e["start"] is not None else float("inf"), e["base"]))
    return out


# ---- シェル統合の nonce 台帳 -------------------------------------------------------------------
# shell/emaki-integration.{zsh,bash} が対話シェルの起動ごとに 1 行追記する:
#   "1 <nonce> <サーバ PID> <ペイン ID> <epoch> <シェルの PID> [<親シェルの nonce>]"
#   （tmux の外なら PID・ペインは "-"。親は、そのシェルを起動したシェル（入れ子・exec）の nonce か "-"）
# シェルは自分の印（OSC 133 / 633;E）に ";emaki=<nonce>:<コマンド番号>" を付けるので、同じサーバ・
# 同じペインで登録された nonce を持つ印だけを本物として扱える。

def _registry_paths():
    p = os.environ.get("EMAKI_NONCE_FILE")
    if p:
        return [os.path.expanduser(p)]
    out = []
    # キーバインド（run-shell）から動くときは tmux サーバの環境変数になり、シェルと
    # XDG_STATE_HOME が食い違うことがあるので、既定の場所も合わせて読む
    for st in (os.environ.get("XDG_STATE_HOME"),
               os.path.join(os.path.expanduser("~"), ".local", "state")):
        q = os.path.join(st, "emaki", "nonces") if st else None
        if q and q not in out:
            out.append(q)
    return out


def nonce_registry_path():
    """台帳の場所: $EMAKI_NONCE_FILE、無ければ ${XDG_STATE_HOME:-~/.local/state}/emaki/nonces"""
    return _registry_paths()[0]


def load_nonce_registry(path=None):
    """台帳を読んで [{nonce, server_pid, pane, epoch, pid, parent, order}] を返す
    （壊れた行は無視。無ければ []）。order は台帳に書かれた順（シェルが起動した順。時計に依らない）。"""
    if path is None:
        out = []
        for p in _registry_paths():
            out.extend(load_nonce_registry(p))
        for i, r in enumerate(out):
            r["order"] = i
        return out
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            cut = size > _NONCE_MAX
            f.seek(size - _NONCE_MAX if cut else 0)
            data = f.read(_NONCE_MAX)
    except (OSError, ValueError):
        return []
    lines = data.split(b"\n")
    if cut:
        lines = lines[1:]  # 途中から読んだ先頭行は欠けている
    out = []
    for ln in lines:
        f = ln.split()
        if len(f) not in (6, 7) or f[0] != b"1":
            continue
        try:
            nonce, spid, pane, epoch, pid = (x.decode("ascii") for x in f[1:6])
            parent = f[6].decode("ascii") if len(f) == 7 else "-"
        except UnicodeDecodeError:
            continue
        if not _NONCE_RE.match(nonce) or not epoch.isdigit() or not pid.isdigit() \
                or len(epoch) > 12 or len(pid) > 10:
            continue
        if parent == "-":
            parent = None
        elif not _NONCE_RE.match(parent):
            continue
        if spid == "-":
            spid = None
        elif not spid.isdigit() or len(spid) > 10:
            continue
        if pane == "-":
            pane = None
        elif not _PANE_RE.match(pane):
            continue
        out.append({"nonce": nonce, "server_pid": spid, "pane": pane, "epoch": int(epoch),
                    "pid": int(pid), "parent": parent, "order": len(out)})
    return out


def nonces_for(entry, registry):
    """そのログ（ペイン）で本物として扱う nonce → {"order": 台帳での順番, "parent": 親の nonce}。

    サーバ PID とペイン ID の両方が分かり、両方一致する登録だけ。索引の無い古いログは
    サーバが分からないので {}（シェル統合の印は使わない）。
    """
    spid, pane = entry.get("server_pid"), entry.get("pane")
    if not spid or not pane:
        return {}
    out = {}
    for r in registry:
        if r["pane"] == pane and r["server_pid"] == spid and r["nonce"] not in out:
            out[r["nonce"]] = {"order": r.get("order", 0), "parent": r.get("parent")}
    return out


def load_raw(e):
    """raw を読む。完成した .raw.log があればそれ、無ければ分割中の part を順に連結。"""
    rb = e["rawbase"] or e["base"]
    p = e.get("rawfile") or rb + ".raw.log"
    try:
        if os.path.isfile(p):
            with open(p, "rb") as f:
                return f.read()
        parts = sorted(glob.glob(glob.escape(rb) + ".raw.log.part-*"))
        if parts:
            chunks = []
            for x in parts:
                with open(x, "rb") as f:
                    chunks.append(f.read())
            return b"".join(chunks)
    except OSError:
        return None
    return None


def load_clean(e):
    p = e["base"] + ".log"
    try:
        with open(p, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def capture_pane(pane_id):
    return tmux("capture-pane", "-p", "-e", "-J", "-S", "-", "-E", "-", "-t", pane_id, raw=True)
