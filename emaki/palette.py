"""端末の配色（16 色・既定の文字色と背景色）を、使っている端末の設定から読む。

ログに残っているのは「1 番の色（赤）」のような番号だけで、実際の色は端末の配色で決まる。HTML でも
端末と同じ色にするため、書き出すときに配色を読んで HTML に埋め込む。

QTerminal（Kali の標準の端末）: 設定（qterminal.org/qterminal.ini）の colorScheme の名前の
配色ファイル（<名前>.colorscheme）を、QTerminal と同じ場所・同じ順で探す。読めなければ Kali の標準の
配色（Kali-Dark）。
"""
import os
import re

# Kali の QTerminal の標準の配色（kali-themes の share/qtermwidget6/color-schemes/Kali-Dark.colorscheme）
KALI_DARK = {
    "name": "Kali-Dark",
    "fg": "#ffffff", "bg": "#23252e", "fgi": "#ffffff", "bgi": "#23252e",
    "c": ["#1f2229", "#d41919", "#5ebdab", "#fea44c", "#367bf0", "#9755b3", "#49aee6", "#e6e6e6",
          "#198388", "#ec0101", "#47d4b9", "#ff8a18", "#277fff", "#962ac3", "#05a1f7", "#ffffff"],
    "bi": True,
}

_NAME = re.compile(r"^[\w .+-]{1,80}$")   # 配色の名前（パスの区切りなどは受け付けない）
_SECTION = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_RGB = re.compile(r"^\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*$")
_HEX = re.compile(r"^\s*#([0-9a-fA-F]{6})\s*$")


def _dirs(var, default):
    out = []
    for d in (os.environ.get(var) or default).split(os.pathsep):
        if d and os.path.isabs(d) and d not in out:
            out.append(d)
    return out


def _config_dirs():
    home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return [home] + _dirs("XDG_CONFIG_DIRS", "/etc/xdg")


def _data_dirs():
    home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return [home] + _dirs("XDG_DATA_DIRS", "/usr/local/share" + os.pathsep + "/usr/share")


def qterminal_scheme():
    """QTerminal の設定にある配色の名前。利用者の設定 → システム全体の設定（/etc/xdg。Kali はここで
    Kali-Dark を指定している）の順に、指定のある最初のものを使う（QTerminal と同じ）。
    設定はあるがどこにも指定が無ければ QTerminal の既定（Linux）、設定が無ければ None。"""
    found = False
    for d in _config_dirs():
        path = os.path.join(d, "qterminal.org", "qterminal.ini")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read(1 << 20)
        except OSError:
            continue
        found = True
        m = re.search(r"^\s*colorScheme\s*=\s*\"?([^\"\r\n]*?)\"?\s*$", text, re.M)
        if m and m.group(1):
            return m.group(1)
    return "Linux" if found else None


def _scheme_dirs():
    """QTerminal が配色ファイルを読むディレクトリ。同じ名前の配色は先にあるものが使われる（QTerminal と同じ）。
    qtermwidget の配色（/usr/share/qtermwidget6/color-schemes。Qt5 の版は qtermwidget5）→
    <データのディレクトリ>/qterminal/color-schemes（~/.local/share → /usr/local/share → /usr/share）→
    設定ファイルのあるディレクトリ（~/.config/qterminal.org）/color-schemes"""
    sysdata = _dirs("XDG_DATA_DIRS", "/usr/local/share" + os.pathsep + "/usr/share")
    out = [os.path.join(d, "qtermwidget6", "color-schemes") for d in sysdata]
    if not any(os.path.isdir(d) for d in out):
        out = [os.path.join(d, "qtermwidget5", "color-schemes") for d in sysdata]
    out += [os.path.join(d, "qterminal", "color-schemes") for d in _data_dirs()]
    conf = os.path.join(_config_dirs()[0], "qterminal.org")
    if os.path.isfile(os.path.join(conf, "qterminal.ini")):
        out.append(os.path.join(conf, "color-schemes"))
    return out


def find_scheme(name):
    """配色ファイル <name>.colorscheme を、QTerminal が読む場所から探す。"""
    if not name or not _NAME.match(name) or name.startswith("."):
        return None
    for d in _scheme_dirs():
        path = os.path.join(d, name + ".colorscheme")
        if os.path.isfile(path):
            return path
    return None


def parse_colorscheme(path):
    """qtermwidget / Konsole の .colorscheme（[Color0] Color=R,G,B など）→ 配色。色が 1 つも無ければ None。
    書かれていない色・読めない色は黒（qtermwidget と同じ）。"""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read(1 << 20)
    except OSError:
        return None
    sec = None
    col = {}
    desc = None
    for line in text.splitlines():
        m = _SECTION.match(line)
        if m:
            sec = m.group(1).strip()
            continue
        k, sep, v = line.partition("=")
        if not sep or sec is None:
            continue
        k = k.strip()
        if sec == "General" and k == "Description":
            desc = v.strip()[:80]
        if k != "Color":
            continue
        m = _RGB.match(v)
        if m and all(int(x) <= 255 for x in m.groups()):
            col[sec] = "#%02x%02x%02x" % tuple(int(x) for x in m.groups())
            continue
        m = _HEX.match(v)
        if m:
            col[sec] = "#" + m.group(1).lower()
    keys = (["Foreground", "Background", "ForegroundIntense", "BackgroundIntense"]
            + ["Color%d%s" % (i, x) for x in ("", "Intense") for i in range(8)])
    if not any(k in col for k in keys):
        return None
    c = {k: col.get(k, "#000000") for k in keys}
    name = os.path.splitext(os.path.basename(path))[0]
    return {
        "name": desc or name,
        "fg": c["Foreground"], "bg": c["Background"],
        "fgi": c["ForegroundIntense"], "bgi": c["BackgroundIntense"],
        "c": [c["Color%d" % i] for i in range(8)] + [c["Color%dIntense" % i] for i in range(8)],
        # qtermwidget（QTerminal）は太字の文字を、0〜7 番の色・既定の文字色なら明るい側の色で描く
        "bi": True,
    }


def load_palette(spec=None):
    """配色を決める。戻り値: (配色 or None, 説明, 問題)。None = HTML の既定の配色を使う。例外は投げない。
    問題: 指定された配色・QTerminal の配色が読めず、Kali-Dark にしたときの理由（HTML の注記に出す）。それ以外は None。

    spec: 空 / "auto" = QTerminal の設定から（読めなければ Kali-Dark）、"emaki" = HTML の既定の配色、
          配色ファイルのパス（.colorscheme）、または配色の名前（QTerminal と同じ場所から探す）。
    """
    spec = (spec or "").strip()
    try:
        if spec.lower() == "emaki":
            return None, "emaki の既定", None
        if spec and spec.lower() != "auto":
            path = os.path.expanduser(spec)
            if not (os.sep in spec or "/" in spec or spec.endswith(".colorscheme")):
                path = find_scheme(spec)
            pal = parse_colorscheme(path) if path else None
            if pal:
                return pal, "%s（%s）" % (pal["name"], path), None
            why = "指定された配色 %s が読めなかった" % spec
            return dict(KALI_DARK), "Kali-Dark（%sため Kali の標準）" % why, why
        name = qterminal_scheme()
        if name:
            path = find_scheme(name)
            pal = parse_colorscheme(path) if path else None
            if pal:
                return pal, "QTerminal の %s（%s）" % (pal["name"], path), None
            if name == "Kali-Dark":
                return dict(KALI_DARK), "QTerminal の Kali-Dark", None
            why = "QTerminal の配色 %s が見つからなかった" % name
            return dict(KALI_DARK), "Kali-Dark（%sため Kali の標準）" % why, why
        return dict(KALI_DARK), "Kali-Dark（QTerminal の設定が無いため Kali の標準）", None
    except Exception:  # 配色は見た目だけなので、何があっても書き出しは止めない
        why = "配色を読めなかった"
        return dict(KALI_DARK), "Kali-Dark（%sため Kali の標準）" % why, why
