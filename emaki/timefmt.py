"""@rl_timestamp_format（strftime 形式）で付けた行頭の日時を読み戻す。

raw が残っていない（keep_raw off で finalize 済み）ログは整形済み .log から読むしかないので、
行頭のプレフィックスから時刻を復元する。対応していない指示子があれば None（時刻なし扱い）。
"""

import datetime
import re
import time

DEFAULT = "[%Y-%m-%d %H:%M:%S] "
# 行頭を日時として読むのに必要な一致率（空行を除く。sanitize.pl は空行と、折り返しで複数行に
# なった行の 2 行目以降には日時を付けない）。書式が記録されているログは緩めに、書式が分からない
# 古いログは「ほぼ全行に付いている」ときだけ読む（出力に元からある日時を消さないように）
NEED_KNOWN = 0.5
NEED_UNKNOWN = 0.9
_EXPAND = {"F": "%Y-%m-%d", "T": "%H:%M:%S", "D": "%m/%d/%y", "R": "%H:%M"}
_DIR = {
    "Y": ("Y", r"\d{4}"), "y": ("y", r"\d{2}"), "m": ("m", r"\d{1,2}"), "d": ("d", r"\d{1,2}"),
    "e": ("d", r"\s?\d{1,2}"), "H": ("H", r"\d{1,2}"), "M": ("M", r"\d{1,2}"),
    "S": ("S", r"\d{1,2}"), "s": ("s", r"\d{1,12}"), "b": ("b", r"[A-Za-z]{3}"),
    "a": (None, r"[A-Za-z]{3}"), "A": (None, r"[A-Za-z]+"), "z": (None, r"[+-]\d{4}"),
    "Z": (None, r"[A-Za-z][A-Za-z0-9+-]{0,8}"), "j": (None, r"\d{3}"),
}
_MON = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_BACK = 3600  # これより大きく時刻が戻ったら、欠けている日付を 1 つ進める（夏時間の 1 時間戻しは許す）


def _date_of(ref):
    try:
        if ref is not None:
            return datetime.date.fromtimestamp(ref)
    except (ValueError, OverflowError, OSError):
        pass
    return datetime.date.today()


def _bump(d, unit):
    if unit == "d":
        return d + datetime.timedelta(days=1)
    if unit == "m":  # 日は行から取るので 1 日に寄せておく（1/31 → 2/31 のような無い日を作らない）
        return d.replace(year=d.year + 1, month=1, day=1) if d.month == 12 else \
            d.replace(month=d.month + 1, day=1)
    return d.replace(year=d.year + 1, day=1)


def tsfmt_parser(fmt, ref=None):
    """(compiled regex, parse(str) -> epoch or None) を返す。作れなければ None。

    日付の欠けた書式（"[%H:%M:%S] " など）は、ref（ログの開始時刻。無ければ今日）の日付から始め、
    時刻が 1 時間より大きく戻るたびに日付を 1 日進める（年や月だけが欠けている書式なら年・月を
    進める）。そのため parse はファイルの先頭から順に呼ぶこと（ログ 1 本ごとに作り直す）。
    """
    fmt = fmt or DEFAULT
    # %F などを展開（"%%F" は文字どおりの "%F" なので展開しない）
    fmt = re.sub(r"%(.)", lambda m: _EXPAND.get(m.group(1), m.group(0)), fmt, flags=re.S)
    rx = []
    used = set()
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch == "%" and i + 1 < len(fmt):
            d = fmt[i + 1]
            i += 2
            if d == "%":
                rx.append("%")
                continue
            if d not in _DIR:
                return None
            name, pat = _DIR[d]
            if name and name not in used:
                used.add(name)
                rx.append("(?P<%s>%s)" % (name, pat))
            else:
                rx.append("(?:%s)" % pat)
        else:
            rx.append(re.escape(ch))
            i += 1
    if not used:
        return None
    regex = re.compile("".join(rx))
    # 書式に無い日付の単位のうち一番細かいもの（時刻が戻ったときに進める単位）
    unit = None
    if "s" not in used:
        if "d" not in used:
            unit = "d"
        elif "m" not in used and "b" not in used:
            unit = "m"
        elif "Y" not in used and "y" not in used:
            unit = "y"
    st = {"cur": _date_of(ref), "prev": ref}

    def parse(s):
        m = regex.match(s)
        if not m:
            return None
        g = m.groupdict()
        try:
            if g.get("s"):
                return int(g["s"])
            if g.get("Y"):
                year = int(g["Y"])
            elif g.get("y"):
                year = 2000 + int(g["y"])
            else:
                year = None
            if g.get("m"):
                mon = int(g["m"])
            elif g.get("b"):
                mon = _MON.get(g["b"].lower(), 0)
            else:
                mon = None
            day = int(g["d"]) if g.get("d") else None
            hms = (int(g.get("H") or 0), int(g.get("M") or 0), int(g.get("S") or 0))
            if hms[0] > 23 or hms[1] > 59 or hms[2] > 61:
                return None

            def mk(c):
                mo = c.month if mon is None else mon
                dd = c.day if day is None else day
                if not (1 <= mo <= 12 and 1 <= dd <= 31):
                    return None
                return int(time.mktime((c.year if year is None else year, mo, dd) + hms
                                       + (0, 0, -1)))

            cur = st["cur"]
            t = mk(cur)
            if t is None:
                return None
            prev = st["prev"]
            n = 0
            while unit and prev is not None and t < prev - _BACK and n < 3:
                cur = _bump(cur, unit)
                t = mk(cur)
                n += 1
            st["cur"], st["prev"] = cur, t
            return t
        except (ValueError, OverflowError, TypeError):
            return None

    return regex, parse


def clean_log_parser(text, fmt, ref=None):
    """整形済み .log（text）の行頭日時を読む (regex, parse) を返す。読まないなら None。

    fmt: 記録時の書式（索引・ペインオプションの tsfmt）。
      "none" … 日時なしで記録した。行頭は一切読まない（出力に元からある日時を消さない）
      None   … 不明（索引の無い古いログ）。既定の書式で、空でない行のほぼ全部が一致するときだけ読む
    ref: ログの開始時刻（日付の欠けた書式の基準日）。
    """
    if fmt == "none":
        return None
    p = tsfmt_parser(fmt or DEFAULT, ref)
    if p is None:
        return None
    need = NEED_KNOWN if fmt else NEED_UNKNOWN
    total = hit = 0
    match = p[0].match
    for ln in text.split("\n"):
        if ln.strip():
            total += 1
            if match(ln):
                hit += 1
    if not total or hit < need * total:
        return None
    return p
