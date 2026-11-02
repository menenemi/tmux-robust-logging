"""論理行の列を「コマンド単位のブロック」に分ける。

方式（ペインごとに自動選択）:
  exact    シェル統合（OSC 133 C/D）の本物の印が raw にある → 印の通りに正確に分割。
           終了コードは D から、コマンド文字列は OSC 633;E から取る
  readline 印は無いが、zsh/bash が入力受付時に出す ESC[?2004h がある → その行を
           「プロンプト＋入力」とみなし、その位置より右を入力文字列とする
           （bash の readline などは印をプロンプトより前に出すので、その場合はプロンプトの形で分ける）
  regex    どちらも無い（整形済み .log しか無い・別のシェル等）→ プロンプトの形で判定

さらに evil-winrm / ssh / nc / msfconsole などの対話ツールの出力の中は、リモート側の
プロンプトでもう一段分割する（入れ子ブロック）。
"""

import bisect
import re

from .parse import Line, line_chunks
from .render import cells_to_index, text_width

# ---- プロンプトの形 -----------------------------------------------------------------------
# (表示名, 正規表現, 対象ツール)  対象ツール None = どの対話ツールの中でも可
_P = re.compile
REMOTE_PROMPTS = [
    ("Evil-WinRM", _P(r"^(?P<p>\*Evil-WinRM\*\s+PS\s+[^>\n]{1,300}>\s?)(?P<c>.*)$"), None),
    ("PowerShell", _P(r"^(?P<p>(?:\[[^\]\n]{1,100}\]:\s*)?PS\s+[^>\n]{1,300}>\s?)(?P<c>.*)$"), None),
    ("meterpreter", _P(r"^(?P<p>meterpreter\s*>\s?)(?P<c>.*)$"), None),
    ("cmd.exe", _P(r"^(?P<p>[A-Za-z]:\\[^<>|\n]{0,300}>)(?P<c>.*)$"), None),
    ("msfconsole", _P(r"^(?P<p>msf\d*(?:\s+[\w/.-]{1,200}\([^)\n]{0,200}\))?\s*>\s?)(?P<c>.*)$"),
     {"msfconsole"}),
    ("sliver", _P(r"^(?P<p>sliver(?:\s+\([^)\n]{0,100}\))?\s*>\s?)(?P<c>.*)$"), {"sliver", "sliver-client"}),
    ("smbclient", _P(r"^(?P<p>smb:\s*\\[^>\n]{0,300}>\s?)(?P<c>.*)$"), {"smbclient"}),
    ("ftp", _P(r"^(?P<p>s?ftp>\s?)(?P<c>.*)$"), {"ftp", "sftp", "lftp"}),
    ("python", _P(r"^(?P<p>>>>\s)(?P<c>.*)$"), {"python", "python2", "python3", "ipython", "ipython3"}),
    ("mysql", _P(r"^(?P<p>(?:mysql|MariaDB\s+\[[^\]\n]{0,100}\])>\s?)(?P<c>.*)$"), {"mysql", "mariadb"}),
    ("MSSQL", _P(r"^(?P<p>SQL(?:\s*\([^)\n]{0,100}\))?>\s?)(?P<c>.*)$"),
     {"mssqlclient.py", "impacket-mssqlclient"}),
    ("psql", _P(r"^(?P<p>[\w-]{1,63}=[#>]\s?)(?P<c>.*)$"), {"psql"}),
    ("sqlite", _P(r"^(?P<p>sqlite>\s?)(?P<c>.*)$"), {"sqlite3", "sqlite"}),
    ("redis", _P(r"^(?P<p>[\w.:-]{1,100}:\d{1,5}(?:\[\d+\])?>\s?)(?P<c>.*)$"), {"redis-cli"}),
    ("shell", _P(r"^(?P<p>(?:\([^)\n]{1,60}\)\s*)?[\w.-]{1,64}@[\w.-]{1,253}(?::[^\n$#]{0,300})?\s?[$#]\s)"
                 r"(?P<c>.*)$"), None),
    ("shell", _P(r"^(?P<p>\[[\w.-]{1,64}@[\w.-]{1,253}(?:[^\]\n\w.-][^\]\n]{0,299})?\][$#]\s)(?P<c>.*)$"),
     None),
    ("shell", _P(r"^(?P<p>└[─╼]*\s?[$#]\s)(?P<c>.*)$"), None),   # kali: └─$ / Parrot: └──╼ $
    ("shell", _P(r"^(?P<p>(?:sh|bash|zsh|ksh|dash)-[\d.]{1,10}[$#]\s)(?P<c>.*)$"), None),
    ("shell", _P(r"^(?P<p>[$#]\s)(?P<c>.*)$"),                              # nc 等の素の sh
     {"nc", "ncat", "netcat", "socat", "pwncat", "pwncat-cs", "telnet"}),
]
LOCAL_PROMPTS = [x for x in REMOTE_PROMPTS if x[2] is None] + [
    ("shell", _P(r"^(?P<p>.{0,300}?[❯➜➤›»λ]\s)(?P<c>.*)$"), None),  # ❯ ➜ ➤ › » λ
    ("shell", _P(r"^(?P<p>[\w.-]{1,64}%\s)(?P<c>.*)$"), None),           # zsh 既定: host%
    ("shell", _P(r"^(?P<p>\$\s)(?P<c>.*)$"), None),
]
# 対話ツール（REPL）の入力行のプロンプト。入力受付の印（ESC[?2004h）がある行にだけ使う
# （出力の中の「>>> 」などを偽のコマンドにしないため）
REPL_PROMPTS = [
    ("gdb", _P(r"^(?P<p>\((?:gdb|Pdb|lldb)\)\s?)(?P<c>.*)$"), None),
    ("gdb", _P(r"^(?P<p>(?:pwndbg|gef)(?:➤|>)\s?)(?P<c>.*)$"), None),
    ("python", _P(r"^(?P<p>(?:>>>|\.\.\.)\s)(?P<c>.*)$"), None),
    ("ipython", _P(r"^(?P<p>In \[\d+\]:\s)(?P<c>.*)$"), None),
    ("php", _P(r"^(?P<p>php >\s?)(?P<c>.*)$"), None),
    ("irb", _P(r"^(?P<p>irb\([^)\n]{0,60}\):\d+:\d+[>*\"']\s?)(?P<c>.*)$"), None),
    ("smbclient", _P(r"^(?P<p>#\s)(?P<c>.*)$"), None),            # impacket-smbclient
    ("REPL", _P(r"^(?P<p>[\w.-]{1,40}>\s)(?P<c>.*)$"), None),     # node / mongo / sqlmap 等
]
_PROMPT_TOP = _P(r"^\s*[┌╭]")                                  # 2 行プロンプトの上段 ┌ ╭
_BG_JOB = _P(r"(?<![&|])&[!|]?\s*$|^\s*bg\b")                 # 背景ジョブを起動するコマンド（cmd & / bg）
_JOB_NOTICE = _P(r"^\[\d+\]\s*[-+]?\s+(?:done|exit|killed|terminated|stopped|suspended|running|hangup"
                 r"|interrupt|continued|segmentation fault|\d+\s)")  # zsh / bash のジョブ通知
# 継続行のプロンプト（zsh の PS2 は「for> 」「dquote> 」など、bash の PS2 は「> 」）
_PS2 = _P(r"^(?:[a-z]+\s)*[a-z]*>$")
_PS2_LINE = _P(r"^(?P<p>(?:[a-z]+\s)*[a-z]*>\s)(?P<c>.*)$")

# 中をリモート側プロンプトで分割する対象（コマンドの先頭語）
INTERACTIVE = {
    "evil-winrm", "evil-winrm.rb", "ssh", "telnet", "nc", "ncat", "netcat", "socat", "pwncat",
    "pwncat-cs", "msfconsole", "python", "python2", "python3", "ipython", "ipython3", "irb", "node",
    "mysql", "mariadb", "psql", "sqlite3", "sqlite", "mongo", "mongosh", "redis-cli", "ftp", "sftp",
    "lftp", "smbclient", "rpcclient", "docker", "podman", "kubectl", "sqlplus", "powershell", "pwsh",
    "cmd", "cmd.exe", "bash", "sh", "zsh", "dash", "ksh", "fish", "su", "screen", "sliver",
    "sliver-client", "impacket-psexec", "impacket-smbexec", "impacket-wmiexec", "impacket-dcomexec",
    "impacket-atexec", "impacket-mssqlclient", "psexec.py", "smbexec.py", "wmiexec.py",
    "dcomexec.py", "atexec.py", "mssqlclient.py", "winrm", "ligolo-ng", "proxy",
}
_INTERACTIVE_RX = _P(r"^(?:(?:python|pypy)\d?(?:\.\d+)?|ipython\d?|bpython|ptpython|php\d?(?:\.\d+)?|ruby"
                     r"|irb\d?|node\d*|lua\d?(?:\.\d+)?|gdb|gdb-multiarch|pwndbg|gef|lldb|r2|radare2"
                     r"|impacket-[\w.-]+|[\w-]+\.py|sqlmap|mongo(?:sh)?|mysql\w*|sqlcmd)$")


def is_interactive(tool):
    """中でリモート側・REPL のプロンプトが出るツールか（入れ子に分ける対象）。"""
    return tool in INTERACTIVE or bool(tool and _INTERACTIVE_RX.match(tool))


_PREFIX_CMDS = {
    "sudo", "doas", "env", "time", "nohup", "proxychains", "proxychains4", "proxychains-ng", "rlwrap",
    "stdbuf", "exec", "command", "builtin", "nice", "ionice", "timeout", "torsocks", "unbuffer",
    "winpty", "noglob",
}
_SUDO_ARG_OPTS = {"-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-T", "-D"}
_ASSIGN = _P(r"^[A-Za-z_]\w*=")


def tool_of(cmd):
    """コマンド文字列の実質的な先頭語（sudo / rlwrap / proxychains / VAR=val などを飛ばす）。"""
    toks = cmd.strip().split()
    i = 0
    first = toks[0].rsplit("/", 1)[-1] if toks else ""
    while i < len(toks):
        base = toks[i].rsplit("/", 1)[-1]
        if base in _PREFIX_CMDS:
            i += 1
            while i < len(toks):
                t = toks[i]
                if base in ("sudo", "doas") and t in _SUDO_ARG_OPTS:
                    i += 2
                elif t.startswith("-") or (base == "timeout" and t[:1].isdigit()):
                    i += 1
                else:
                    break
            continue
        if _ASSIGN.match(toks[i]):
            i += 1
            continue
        return base
    return "su" if first in ("sudo", "doas", "su") else ""


# ---- 右プロンプト（p10k の ✘ 127 / 時刻 / took 3s など）---------------------------------
_RP = _P(r"^(?P<body>.*\S)?(?P<gap>\s{3,})(?P<tail>\S.{0,60}?)\s*$")
# 隙間が 1〜2 個しか無いとき（入力が右プロンプトの直前まで伸びた）は、右端に揃っていて
# 状態の記号から始まる部分だけを右プロンプトとみなす
_RP_EDGE = _P(r"^(?P<body>.*?\S)(?P<gap>\s{1,2})(?P<tail>[✘✗×✖✔✓].{0,60}?)\s*$")
_RP_STATUS = _P(r"[✘✗×✖✔✓]|\b\d{1,2}:\d{2}(?::\d{2})?\b|\btook\b"
                r"|\b\d+(?:\.\d+)?(?:ms|s|m)\b")
_RP_FAIL = _P(r"[✘✗×✖]\s*(\d{1,3}|[A-Z]{3,9})?")
_RP_OK = _P(r"[✔✓]")


def strip_rprompt(s, width=None, row_width=None, aligned=False):
    """行末の右プロンプトを取り除き、そこに出ていた終了コードを返す。
    p10k の status は「直前に実行したコマンド」の終了コードなので、呼び出し側で 1 つ前に付け替える。
    width / row_width: 端末幅とその行の表示幅（分かれば、隙間の狭い右プロンプトも拾う）
    aligned: 端末幅が確かな zsh の入力行（PROMPT_SP の幅で、その後に描き直されていない）。zle は右プロンプトを
             右端に揃えて描き、入力が折り返した行には出さないので、右端近くまで届いていない末尾は削らない
             （入力や背景ジョブの出力の途中にある「空白 3 つ以上 + 時刻」「… 3 failed   ✘ 1」など）
    戻り値: (本体, exit, 右プロンプトの幅, 隙間を含めて取り除いた幅)
            exit: int / -1（失敗だがコード不明・シグナル）/ None。右プロンプトが無ければ幅は 0
    """
    if len(s) > 1000:
        return s, None, 0, 0  # 長い入力行には右プロンプトは出ない（zle が消す）
    edge = bool(width) and row_width is not None and row_width >= width - 2
    m = _RP.match(s)
    if not m or not _RP_STATUS.search(m.group("tail")):
        m = _RP_EDGE.match(s) if edge else None
        if not m:
            return s, None, 0, 0
    # 右端の 1 桁を空ける ZLE_RPROMPT_INDENT（既定 1）が多少大きくてもよいよう、4 桁までの隙間は認める
    if aligned and not (row_width is not None and row_width >= width - 4):
        return s, None, 0, 0
    tail = m.group("tail").rstrip()
    tw = text_width(tail)
    return m.group("body") or "", _rp_exit(tail), tw, tw + text_width(m.group("gap"))


def _rp_exit(tail):
    """右プロンプトの文字列 → そこに出ている終了コード（int / -1（失敗だがコード不明）/ None）。"""
    mf = _RP_FAIL.search(tail)
    if mf:
        code = mf.group(1)
        return int(code) if code and code.isdigit() else -1
    return 0 if _RP_OK.search(tail) else None


_PROMPT_CHARS = _P(r"[$#>%❯➜➤›»λ]")   # どのプロンプトの形にも必ず含まれる記号（大量の出力行を素早く除外）
_PROMPT_END = _P(r"[$#>%❯➜➤›»λ]\s*$")  # プロンプトの終わりらしい（描き直した「プロンプト + 入力」の行の左側）
# zle の状態行（入力行の下に出し、入力を確定する前に消すもの）: 履歴検索・execute-named-cmd
_ZLE_STATUS = _P(r"(?:failing )?(?:bck|fwd)-i-search:|execute: ")


def prompt_split(text, table, tool=None):
    """プロンプトの形にマッチすれば (表示名, プロンプト, コマンド) を返す。"""
    first = text.split("\n", 1)[0]
    if not _PROMPT_CHARS.search(first):
        return None
    for label, rx, tools in table:
        if tools is not None and tool not in tools:
            continue
        m = rx.match(first)
        if m:
            return label, m.group("p"), m.group("c")
    return None


def _zle_nl(L):
    """zle が自分で次の行へ移った行か（CR NL。端末に届くときは CR CR LF）。補完の一覧・履歴検索・複数行の入力・
    ジョブの通知の前など。背景ジョブなどの出力は NL だけなので CR LF になる。"""
    return L.ev.get("eol", 0) >= 2


def _out_nl(L):
    """出力の改行（CR LF）で終わった行か（_zle_nl の逆。ログの最後の行のように改行が無い行は含めない）。"""
    return L.ev.get("eol", 0) == 1


def _zle_free(L):
    """zle が自分で次の行へ移っていない・カーソルを上へ戻して描き直していない入力行か（入力行の右に見えるものが、
    打った文字や zle の表示ではなく、割り込んだ出力でありうる）。"""
    return not _zle_nl(L) and not L.ev.get("up")


def _is_top(L):
    """2 行プロンプトの上段らしい行か（PROMPT_SP かシェル統合の A の印で始まる行、または ┌ ╭ の行）。"""
    return L.alt is None and not _blank(L) and bool(
        L.ev.get("psp") or _PROMPT_TOP.match(L.text) or any(mk[0] == "A" for mk in L.marks))


def _rows_line(L, rows):
    """入力行の下段に書かれた出力（入力確定後に zle がカーソルを下げて書いたもの）を 1 行にする。"""
    x = Line(L.s, L.e, None, [])
    x.rows = rows
    x.text = "\n".join("".join(t for t, _ in row) for row in rows)
    x.t0 = x.t1 = L.t1
    return x


def _text_line(text, ref):
    """文字列だけの行（色なし）。入力行の右に混ざった背景ジョブの出力などを出力側へ回すのに使う。"""
    x = Line(ref.s, ref.e, None, [])
    x.rows = [[(text, 0)]] if text else [[]]
    x.text = text
    x.t0, x.t1 = ref.t0, ref.t1
    return x


def _acc_text(L, strip=True):
    """ESC[?2004l（入力確定）のある段の、確定位置までの文字列。strip=False なら行頭の空白は残す（続けて打った
    入力の頭の空白・複数行の入力の字下げ）。"""
    texts = ["".join(t for t, _ in row) for row in L.rows] or [""]
    r, c = L.ev["acc"]
    t = texts[r] if r < len(texts) else ""
    t = t[:cells_to_index(t, c)] if c else ""
    return t.strip() if strip else t.rstrip()


# ---- 入力行に割り込んだ背景ジョブの出力（シェル統合が無く、打ったコマンドが分からないとき）------------------
# 打った文字と割り込んだ出力の境目は、読み取りの塊の記録（.tsindex）で分かる（打った文字は 1 打ごとの塊、
# 出力は別の塊）。記録の無いログでは、同じジョブの他の出力の行と形を比べて推定する
_CMD_START = _P(r"[a-z_./~]")  # コマンドの書き始め（ログの行頭の [INFO] ERROR 2026-… などは含まない）


def _shape(s):
    """行の形（数字は同じとみなす。カウンタ・時刻・番号だけが違う背景ジョブの出力の行をそろえる）。"""
    return re.sub(r"\d", "0", s)


def _cpl(a, b):
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _typed_len(text, refs):
    """入力行の右（打ちかけの入力 + 割り込んだ背景ジョブの出力）のうち、打った部分の長さ（0 = 何も打っていない）。
    同じジョブの他の出力の行（refs）と 8 文字以上同じ形で始まる位置を、出力の始まりとみなす。打った部分は
    コマンドの書き始めらしいものだけ。決められなければ None。"""
    rs = [_shape(x.strip()) for x in refs if len(x.strip()) >= 8]
    if not rs:
        return None
    sh = _shape(text)
    if any(_cpl(sh, x) >= 8 for x in rs):
        return 0
    if not _CMD_START.match(text):
        return None
    for k in range(1, min(len(text), 200)):
        if any(_cpl(sh[k:], x) >= 8 for x in rs):
            return k
    return None


def _bg_head_len(text, refs):
    """改行で終わらなかった背景ジョブの出力の後に打った入力（同じ行に続く）→ 出力の部分の長さ（無ければ 0）。
    同じジョブの他の出力の行（refs）と全体が同じ形で、その後ろがコマンドの書き始めらしいときだけ。"""
    best = 0
    sh = _shape(text)
    for x in refs:
        x = x.strip()
        n = len(x)
        if n >= 8 and n > best and len(text) > n and sh[:n] == _shape(x) and _CMD_START.match(text[n:].lstrip()):
            best = n
    return best


def _out_pos(L):
    """改行で終わった行の、最後の出力を書き始めた位置 (row, col)。塊の記録が無ければ None。
    出力が分けて書かれて塊が割れていてもよいよう、同じ秒に届いた 2 文字以上の塊はつなげる。"""
    ch = line_chunks(L)
    if not ch or not _out_nl(L):
        return None
    i = len(ch) - 1
    while i > 0 and not ch[i].vis:
        i -= 1
    if not ch[i].vis:
        return None
    while i > 0 and ch[i - 1].vis > 1 and ch[i - 1].t == ch[i].t:
        i -= 1
    return ch[i].pos


def _between(L, a, b):
    """行 L の位置 a (row, col) から b の手前までの文字列。"""
    rows = ["".join(t for t, _ in row) for row in L.rows] or [""]
    out = []
    for r in range(a[0], min(b[0], len(rows) - 1) + 1):
        t = rows[r]
        out.append(t[cells_to_index(t, a[1]) if r == a[0] else 0:cells_to_index(t, b[1]) if r == b[0] else len(t)])
    return "".join(out)


def _typed_end(L, text, refs=()):
    """入力行の右の文字列 text（打ちかけの入力 + 割り込んだ出力）のうち、打った部分の長さ（0 = 何も打っていない）。
    決められなければ None。"""
    pos = _out_pos(L)
    if pos is None:
        return _typed_len(text, refs)
    start = L.ev.get("b") or L.ev.get("in") or (0, 0)
    if pos <= start:
        return 0
    typed = _between(L, start, pos).lstrip()
    return len(typed) if text.startswith(typed) else _typed_len(text, refs)


def _head_end(L, text, refs):
    """確定行の text（行頭から確定位置まで）のうち、改行で終わらなかった背景ジョブの出力の長さ（0 = 無い）。
    その後ろが打った入力。"""
    vis = [c for c in line_chunks(L) if c.vis]
    if not vis:
        return _bg_head_len(text, refs)
    i = 0
    while i + 1 < len(vis) and vis[i + 1].vis > 1 and vis[i + 1].t == vis[i].t:
        i += 1
    if vis[0].vis < 2 or i + 1 >= len(vis) or vis[i + 1].pos[0] != 0:
        return 0
    return cells_to_index(text, vis[i + 1].pos[1])


def _input_head_end(L, text):
    """入力行の右の text のうち、入力を打つ前に入った、改行で終わらなかった背景ジョブの出力の長さ。塊の記録が
    無ければ None。呼び出した履歴・貼り付けに続けて打った入力と取り違えて本物のコマンドを失わないよう、印の直後の
    塊で、コマンドの書き始めらしくなく、貼り付けの強調も無く、次が 1 打の塊のときだけ。"""
    ch = line_chunks(L)
    if not ch:
        return None
    zi = L.src.find("\x1b[?2004h")
    post = [c for c in ch if c.vis and c.start >= zi]
    if len(post) < 2 or post[0].vis < 3 or post[1].vis != 1 or "\x1b[7m" in L.src[post[0].start:post[0].end]:
        return 0
    head = _between(L, L.ev["in"], post[1].pos).strip()
    return len(head) if text.startswith(head) and not _CMD_START.match(head) else 0


def _split_tail(a, b):
    """a が b（空白の数の違いは同じとみなす）で終わるなら、その前の部分。終わらなければ None。"""
    i, j = len(a.rstrip()), len(b.rstrip())
    while i > 0 and j > 0:
        if a[i - 1].isspace() and b[j - 1].isspace():
            while i > 0 and a[i - 1].isspace():
                i -= 1
            while j > 0 and b[j - 1].isspace():
                j -= 1
        elif a[i - 1] == b[j - 1]:
            i -= 1
            j -= 1
        else:
            return None
    return a[:i] if j == 0 else None


def _strip_head(a, b):
    """a が b（空白の数の違いは同じとみなす）で始まるなら、その後ろの部分。始まらなければ None。"""
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i].isspace() and b[j].isspace():
            while i < len(a) and a[i].isspace():
                i += 1
            while j < len(b) and b[j].isspace():
                j += 1
        elif a[i] == b[j]:
            i += 1
            j += 1
        else:
            return None
    return a[i:] if j >= len(b) else None


def _accepts_here(L):
    """入力確定（ESC[?2004l）がこの行の最後で起きたか（zle は確定後その行に何も書かない）。
    確定の後ろに文字が続く行は、別の形（前の行で入力を終えた readline の確定＋出力）。"""
    if L.alt is not None or "acc" not in L.ev:
        return False
    texts = ["".join(t for t, _ in row) for row in L.rows] or [""]
    r, c = L.ev["acc"]
    if any(x.strip() for x in texts[r + 1:]):
        return False
    t = texts[r] if r < len(texts) else ""
    return not t[cells_to_index(t, c):].strip()


def input_split(L, table=LOCAL_PROMPTS, tool=None):
    """入力受付の印（ESC[?2004h / OSC 133;B）がある行を分ける。

    戻り値: (表示名, プロンプト, コマンド, exit, 出力行 or None, 情報)
    入力行より下の段は、入力確定前に書かれたなら折り返した入力の続き、後なら出力。
    情報: col0（印がプロンプトより前）/ top（2 行プロンプトの上段だけの行）/ noacc（入力確定の印が
          この行に無い）/ ps2（継続行）/ rpw（右プロンプトの幅）/ used（入力行の表示幅）/ W / stale
    """
    texts = ["".join(t for t, _ in row) for row in L.rows] or [""]
    ev = L.ev
    pos = ev.get("b") or ev.get("in")
    r = pos[0] if pos and pos[0] < len(texts) else len(texts) - 1
    post = ev.get("post") or ()
    k = r + 1
    while k < len(texts) and k not in post:
        k += 1
    # 自動折り返しで続いた段は、右端で切り詰められた空白（行末の空白）を補ってからつなぐ
    wraps = ev.get("wrap") or {}
    seg = []
    for x in range(r, k):
        t = texts[x]
        if x + 1 < k and x in wraps:
            t += " " * max(wraps[x] - text_width(t), 0)
        seg.append(t)
    full = "".join(seg)
    extra = _rows_line(L, L.rows[k:]) if any(texts[k:]) else None
    # 行末の空白はレンダリングで切り詰められるので、入力が空のプロンプト（「$ 」で終わる）にも
    # プロンプトの形が合うよう空白を 1 つ補って判定する
    rx = prompt_split(full + " ", table, tool)
    zle = bool(ev.get("zsh"))  # zsh が描いた入力行（印はプロンプトの後ろ）
    # zsh のプロンプトが端末幅より長く、印の段が前の段から自動折り返しで続いている（ちょうど端末幅 + 1 なら、
    # 末尾の空白だけがこの段に来るので、印の左に見える文字が無い）。印が段の頭にあるのは、折り返した出力の
    # 後に CR で戻って印を出したもの（bash の readline など）
    cont = zle and r > 0 and (r - 1) in wraps and bool(pos) and pos[1] > 0
    cand = None
    col0 = False
    if pos and pos[0] < len(texts):
        idx = cells_to_index(texts[r], pos[1])
        cand = (full[:idx], full[idx:])
        # 印の左にプロンプトが無い = 印をプロンプトより前に出すもの（bash の readline / Ruby の reline 等）
        col0 = "b" not in ev and not cand[0].strip() and not cont
    dangle = None
    # zsh は印をプロンプトの後ろに出すので、印より左は必ずプロンプト（入力した「echo 'a » b'」の「» 」を
    # プロンプトと取り違えない）
    if cand is not None and not col0 and "b" not in ev and not zle and cand[0].strip():
        # 印の後ろがプロンプトで始まる = 印をプロンプトより前に出すもの（bash の readline 等）で、
        # 印より左は改行で終わらなかった前のコマンドの出力
        r2 = prompt_split(cand[1] + " ", table, tool)
        if r2 is not None and r2[1].strip() and not _PS2_LINE.match(cand[1]):
            dangle, rx, cand = cand[0], r2, None
        elif _PROMPT_TOP.match(cand[1]):
            # 2 行プロンプトの上段が出力の残りに続いた（bash）: 印は上段の頭にある
            dangle, full, cand, col0 = cand[0], cand[1], ("", cand[1]), True
            rx = prompt_split(full + " ", table, tool)
    use_rx = rx is not None and (
        cand is None
        or ev.get("stale")                       # 入力位置より左が書き換えられた（transient prompt 等）
        or col0
        or (not cand[1].strip() and rx[2].strip()))
    ps2 = False
    if use_rx:
        label, prompt, cmd = rx
    elif cand is not None:
        label, (prompt, cmd) = (rx[0] if rx else "shell"), cand
        if col0:
            m2 = _PS2_LINE.match(full)
            if m2:
                prompt, cmd, ps2 = m2.group("p"), m2.group("c"), True
        else:
            ps2 = bool(_PS2.match(prompt.strip())) and not ev.get("psp")
    else:
        label, prompt, cmd = "shell", "", full
    W = ev.get("w")
    wrapped = k > r + 1
    row_w = text_width(texts[r])
    rp = ev.get("rp")
    if col0 or "b" in ev or dangle is not None:
        ex, rpw, rp_all = None, 0, 0  # bash や REPL に右プロンプトは無い（入力の末尾を削らない）
    elif "rp" in ev and not ev.get("stale") and not use_rx and cand is not None and not wrapped:
        # zle は右プロンプトを入力受付の印より前に描く: 印の時点で入力位置より右に見えていて、最後までそのまま
        # 残ったものだけが右プロンプト（✦ や「1 ⚙」のような状態でないものも）。無ければ右プロンプトは無い
        # （継続行・入力行の右に割り込んだ背景ジョブの出力の末尾にある時刻などを削らない）
        ex, rpw, rp_all = None, 0, 0
        if rp and rp[0] == r:
            cut = cells_to_index(texts[r], rp[1]) - len(full) + len(cmd)
            if 0 <= cut <= len(cmd):
                tail = cmd[cut:].strip()
                cmd = cmd[:cut]
                ex, rpw = _rp_exit(tail), text_width(tail)
                rp_all = row_w - text_width(texts[r][:cells_to_index(texts[r], rp[1])].rstrip())
    else:
        # 端末幅が確か = zsh の PROMPT_SP の幅で、その後にプロンプトが描き直されていない（描き直しは端末幅を
        # 変えたときにも起きる）。確かなら、右プロンプトは右端に揃っているはず
        aligned = bool(W) and zle and not ev.get("stale")
        cmd, ex, rpw, rp_all = strip_rprompt(cmd, W, None if wrapped else row_w, aligned)
    before = "\n".join(texts[:r])
    info = {
        "col0": col0,
        "top": col0 and not ps2 and bool(_PROMPT_TOP.match(full)),
        # zle（印がプロンプトの後ろ）だけ: 入力確定がこの行に無い = 入力受付中に別の出力が割り込んだ
        # （bash の readline は B の印があっても確定を次の行で出すので対象外）
        "noacc": not col0 and "b" not in ev and "in" in ev and "acc" not in ev,
        "ps2": ps2,
        "rpw": rpw,
        "used": W if (wrapped and W) else row_w - rp_all,
        "W": W,
        "stale": bool(ev.get("stale")),
        "dangle": None,
    }
    if dangle is not None:
        # 前のコマンドの出力の残りはプロンプトに含めない（呼び出し側が出力として扱う）
        info["dangle"] = (before + "\n" if before else "") + dangle
        before = ""
    # 印の左に空白しか無い（折り返したプロンプトの末尾の空白）ときは、プロンプトは上の段で終わる
    prompt = "\n".join(x for x in (before, prompt.rstrip()) if x)
    return label, prompt, cmd.strip(), ex, extra, info


def _regex_det(L, table=LOCAL_PROMPTS, tool=None, pad=False, rprompt=True):
    """プロンプトの形だけで判定する（入力受付の印が無い行）。
    pad: プロンプトがあるはずの行（2 行プロンプトの下段など）で、入力が空でも合うよう空白を補う
    rprompt: False なら行末の右プロンプトを探さない（bash に右プロンプトは無い。入力の末尾を削らない）"""
    r = prompt_split(L.text + " " if pad else L.text, table, tool)
    if r is None:
        return None
    c, ex, rpw = r[2], None, 0
    if rprompt:
        c, ex, rpw, _ = strip_rprompt(c)
    return r[0], r[1], c.strip(), ex, None, {"rpw": rpw}


# ---- ブロック -------------------------------------------------------------------------------

def new_block(kind, cmd="", prompt="", t=None, label=None):
    return {"kind": kind, "cmd": cmd, "prompt": prompt, "t": t, "te": None, "exit": None,
            "xi": False, "cwd": None, "exact": False, "running": False, "remote": None,
            "label": label, "lines": [], "children": []}


def _blank(L):
    return L.alt is None and not L.text.strip()


def _has_content(lines):
    return any(not _blank(L) for L in lines)


def _misc_block(lines, label):
    b = new_block("misc", label=label)
    b["lines"] = lines
    b["t"] = next((L.t0 for L in lines if L.t0 is not None), None)
    return b


def _norm_prompt(p):
    """プロンプトの「形」。時刻などの数字を除き、末尾 2 語（パスとプロンプト記号など）で比べる。"""
    last = p.rsplit("\n", 1)[-1]
    return " ".join(re.sub(r"\d+", "", last).split()[-2:])


def _detections(lines, detect, readline, instant=True):
    """プロンプト（＋入力）の行を検出する。戻り値: [(i, j, d, 背景出力)]

    i..j がその検出で使った行。次の場合は後ろの行も使う:
      - 印がプロンプトより前に出るシェルの 2 行プロンプト: 上段の行頭に印があり、入力は下段
      - 入力受付中に背景ジョブの出力が来た: 入力行の右に出力が入り、本当の入力は ESC[?2004l のある行
      - p10k の instant prompt 中に打った入力: プロンプトの次の行に出ることがある（instant=True の
        ときだけ。リモート側の出力の中では、空のプロンプトの次の行を入力とみなさない）
    d = (表示名, プロンプト, コマンド, 右プロンプトの終了コード, 出力行, 情報)
    """
    out = []
    n = len(lines)
    bg = False  # 背景ジョブを起動した（入力受付中に、その出力が割り込むことがある）
    seen = []  # これまでに入力受付中に割り込んだ出力の行（同じジョブの出力と形を比べるのに使う）
    i = 0
    while i < n:
        L = lines[i]
        d = None if L.alt is not None else detect(L)
        if d is None:
            i += 1
            continue
        label, prompt, cmd, ex, extra, info = d
        j = i
        foreign = None
        if info.get("top") and i + 1 < n:
            nx = lines[i + 1]
            if nx.alt is None and detect(nx) is None:
                r2 = _regex_det(nx, pad=True, rprompt=False)
                if r2 is not None:
                    label, prompt, cmd = r2[0], L.text.rstrip() + "\n" + r2[1].rstrip(), r2[2]
                    j = i + 1
        elif readline and info.get("noacc"):
            # 入力受付中に別の出力（背景ジョブ・ジョブの終了通知・長い補完の一覧）が割り込んだ。入力を確定した行
            # （ESC[?2004l）を探す（入力受付の印・PROMPT_SP・全画面アプリの行の手前まで）
            prx = _prompt_rx(prompt)
            k = i + 1
            while k < n and lines[k].alt is None and "acc" not in lines[k].ev and detect(lines[k]) is None \
                    and not lines[k].ev.get("psp"):
                k += 1
            nx = lines[k] if k < n else None
            acc = nx is not None and _accepts_here(nx) and detect(nx) is None
            after = [x for x in lines[i + 1:k] if not _blank(x)]
            t = _norm_ws(_acc_text(nx)) if acc else ""
            m = prx.match(t) if acc and prx is not None else None
            # 複数行の入力（履歴から呼び出した・貼り付けた・ESC Return）: zle が自分で次の行へ移って続きの行を描き、
            # 最後の行で確定する。最後の行の右に背景ジョブの出力が入ったなら、その後の行は出力
            nz = 0
            while i + 1 + nz < k and _zle_nl(lines[i + 1 + nz]):
                nz += 1
            tail = lines[i + 1 + nz:k]
            ml = acc and _zle_nl(L) and not any(x.ev.get("up") for x in [L, nx] + lines[i + 1:k]) and (
                not tail or (bg and _out_nl(tail[0]) and not _JOB_NOTICE.match(tail[0].text)
                             and not _blank(tail[0]) and all(_out_nl(x) or _blank(x) for x in tail)))
            if m:
                # (1) zle がプロンプトを描き直した（通知・長い補完の一覧の後）: 入力は「プロンプト + 入力」の確定行にある
                #     （プロンプトの時刻などは描き直しで変わるので、数字を除いた形で比べる）。2 行プロンプトなら、
                #     確定行の直前の行は描き直した上段（出力ではない）
                typed = t[m.end():].strip()
                rest = _trim_common(cmd, typed) if cmd and _zle_free(L) else ""
                seg = lines[i + 1:k]
                if after and i > 0 and _is_top(lines[i - 1]):
                    trx = _prompt_rx(lines[i - 1].text)
                    if trx is not None and trx.match(_norm_ws(after[-1].text)):
                        seg = seg[:next(x for x, ln in enumerate(seg) if ln is after[-1])]
                        after = after[:-1]
                foreign, cmd, j = ([_text_line(rest, L)] if rest else []) + _interior(seg, bool(rest)) or None, \
                    typed, k
            elif acc and _zle_nl(L) and nx.ev.get("above"):
                # (1b) 入力行の下に zle が出した表示（補完の一覧・履歴検索など）の後、カーソルを入力行へ戻して確定した:
                #      表示の最後の行は確定行に重なっている。戻った後に書いたのは、入力の続きか、描き直した入力全体。
                #      履歴検索などの状態行は確定の前に zle が消している
                row = "".join(x for x, _ in nx.rows[0])
                over, ac = nx.ev.get("over"), nx.ev["acc"]
                rest = row[cells_to_index(row, over):cells_to_index(row, ac[1])] if over is not None and not ac[0] \
                    else ""
                last = "".join(x for x, _ in nx.ev["above"][0]).strip()
                foreign, cmd, j = _interior(lines[i + 1:k]) + (
                    [_rows_line(nx, nx.ev["above"])] if last and not _ZLE_STATUS.match(last) else []) or None, \
                    rest.strip() if rest.strip().startswith(cmd) else (cmd + rest).strip(), k
            elif ml:
                # (2) 複数行の入力: 入力は入力行から確定行まで（字下げは残す）
                body = [cmd] + [x.text.rstrip() for x in lines[i + 1:i + 1 + nz]]
                fl = []
                if tail:
                    last = tail[0].text.rstrip()
                    kx = _typed_end(tail[0], last, [x.text for x in tail[1:]] + seen[-40:])
                    if kx is None:  # 分けられない（入力の最後の行として残す）
                        body.append(last)
                    else:
                        body.append(last[:kx])
                        fl = [_text_line(last[kx:], tail[0])]
                    fl += _interior(tail[1:], True)
                    body[-1] += _acc_text(nx, strip=False)
                else:
                    body.append(_acc_text(nx, strip=False))
                foreign, cmd, j = fl or None, "\n".join(body).rstrip(), k
            elif bg and _out_nl(L) and not L.ev.get("up") \
                    and not any(x.ev.get("up") or _zle_nl(x) for x in lines[i + 1:k]) \
                    and not (acc and nx.ev.get("up")):
                # (3) 背景ジョブの出力が入力行の右に入り、その出力の改行で次の行へ進んだ（zle が自分で次の行へ移った
                #     補完の一覧・履歴検索・複数行の入力・通知や、カーソルを上へ戻して描く表示ではない）: 入力は次の
                #     確定行。入力行の右とそれまでの行は出力。確定行が無いまま次のプロンプトかログの終わりまで来たなら、
                #     コマンドは実行されていない。
                #     入力行の右のうち、出力より前に打っていた部分は、同じジョブの他の出力の行と形を比べて分ける
                refs = [x.text for x in after] + seen[-40:]
                m = nx.ev.get("lclamp") if acc else None
                redrawn = bool(m and cmd and _acc_text(nx).startswith(cmd[:m].strip()))
                if redrawn:
                    # zle が打った入力の頭へ戻って全体を描き直した（オートサジェストの確定など）: 戻ろうとした
                    # 桁数が、出力より前に打っていた部分で、確定行に入力全体がある
                    kx = m
                else:
                    kx = _typed_end(L, cmd, refs) if cmd else None
                typed = cmd[:kx] if kx else ""
                bgt = cmd[len(typed):]
                fl = ([_text_line(bgt, L)] if bgt else []) + _interior(lines[i + 1:k], bool(bgt))
                if acc:
                    rest = _acc_text(nx, strip=False)
                    # 改行で終わらなかった出力の後に打った入力は、確定行でその出力に続く
                    h = _head_end(nx, rest, refs + [bgt])
                    if h:
                        fl.append(_text_line(rest[:h], nx))
                        rest = rest[h:]
                    foreign, cmd, j = fl or None, (rest if redrawn else typed + rest).strip(), k
                elif fl and (nx is None or detect(nx) is not None or nx.ev.get("psp")):
                    if typed:  # 打ったが実行されなかった入力（Ctrl-C で捨てた入力と同じく、プロンプトごと残す）
                        fl.insert(0, _text_line(prompt.rsplit("\n", 1)[-1].rstrip() + " " + typed.strip(), L))
                    foreign, cmd, j = fl, "", k - 1
            elif acc and cmd and not after and k - i <= 4:
                # (4) 描き直さないまま続けて打った: 入力は次の確定行（プロンプト無し）
                foreign, cmd, j = [_text_line(cmd, L)], _acc_text(nx), k
        elif readline and bg and cmd and "in" in L.ev and "acc" in L.ev and not info.get("stale") \
                and not L.ev.get("up"):
            # 改行で終わらなかった背景ジョブの出力の後に、同じ行で打った入力
            h = _input_head_end(L, cmd)
            if h is None:
                h = _bg_head_len(cmd, seen[-40:])
            if h:
                foreign, cmd = [_text_line(cmd[:h], L)], cmd[h:].strip()
        if instant and not cmd and not foreign and readline and j + 1 < n and not info.get("ps2"):
            # p10k の instant prompt 中に打った入力（次の行に出る）。ただしジョブ通知・次のプロンプトの
            # 上段・入力行の直前の行は入力ではない
            nx = lines[j + 1]
            if nx.alt is None and detect(nx) is None and nx.text.strip() \
                    and "\n" not in nx.text and len(nx.text) < 400 \
                    and not _JOB_NOTICE.match(nx.text) and not _PROMPT_TOP.match(nx.text) \
                    and not nx.ev.get("psp") and not (j + 2 < n and detect(lines[j + 2]) is not None):
                cmd = nx.text.strip()
                j += 1
        out.append((i, j, (label, prompt, cmd, ex, extra, info), foreign))
        bg = bg or bool(_BG_JOB.search(cmd))
        if foreign:
            seen.extend(x.text for x in foreign if x.text.strip())
        i = j + 1
    return out


def _interior(seg, lead=False):
    """seg の空でない行と、その間にある出力の空行（出力の改行で終わったもの。zle が自分で次の行へ移った空行は
    含めない）。lead: seg の前にも出力の行がある（入力行の右に入った出力）。"""
    nb = [x for x, L in enumerate(seg) if not _blank(L)]
    if not nb:
        return []
    lo = -1 if lead else nb[0]
    return [L for x, L in enumerate(seg) if not _blank(L) or (lo < x < nb[-1] and L.alt is None and _out_nl(L))]


def _plan(dets):
    """トップレベルのプロンプト行を決める。

    ssh / nc / su などの対話ツールを起動したら、元と同じ形のローカルプロンプトが戻るまでの
    プロンプト行はリモート側とみなし、ここでは区切らない（_nest で入れ子に分ける）。
    """
    out = []
    sig = None
    for det in dets:
        d = det[2]
        if d[5].get("ps2") and sig is None:
            out.append(det)
            continue
        norm = _norm_prompt(d[1])
        if sig is not None:
            if norm != sig:
                continue
            sig = None
        out.append(det)
        if is_interactive(tool_of(d[2])):
            sig = norm
    return out


def _only_blank(lines, a, b):
    """lines[a+1:b] がすべて空行か（a が無効なら False）。"""
    if a < 0:
        return False
    return all(lines[x].alt is None and _blank(lines[x]) for x in range(a + 1, b))


def _top_take(holder, psp_seen):
    """前のブロック（か前置き）の末尾にある 2 行プロンプトの上段を探す → 取り除く行数（0 = 無い）。

    zsh は PS1 の前に PROMPT_SP を出すので、上段はその行か、その後の空行の次の行（Kali の
    NEWLINE_BEFORE_PROMPT は PROMPT_SP の後に空行を 1 つ出す）。PROMPT_SP で始まる行なら枠の有無
    （┌ ╭ の無い p10k lean など）に関係なく上段。PROMPT_SP の出ないシェルでは ┌ ╭ の行だけを上段とみなす。
    """
    if not holder or holder[-1].alt is not None or _blank(holder[-1]):
        return 0
    top = holder[-1]
    if top.ev.get("psp"):
        return 1
    if not _PROMPT_TOP.match(top.text):
        return 0
    if not psp_seen:
        return 1
    k = len(holder) - 2
    while k >= 0 and k >= len(holder) - 4 and holder[k].alt is None and _blank(holder[k]):
        if holder[k].ev.get("psp"):
            return len(holder) - k
        k -= 1
    return 0


def segment_lines(lines, dets):
    """検出したプロンプト行で区切る。戻り値: (前置き行, ブロック)"""
    pre = []
    blocks = []
    cur = None
    n = len(lines)
    starts = {det[0]: det for det in dets}
    # このペインのプロンプトが一度でも状態（✘ N / ✔）を出しているなら、状態の出る余地があったのに
    # 何も出ていないプロンプトは「直前のコマンドは成功」を意味する（p10k は成功時に何も出さない設定が多い）
    shows_status = any(det[2][3] is not None for det in dets)
    # 失敗したときの右プロンプト（✘ 127・took 1m 3s 付きなど）は、これまでに見えたものより広いことがあるので、
    # 余地は少なくとも 12 桁あるときだけ認める
    rp_max = max([det[2][5].get("rpw", 0) for det in dets] + [12])
    # zsh の PROMPT_SP が出ているペインでは、2 行プロンプトの上段は必ず PROMPT_SP で始まる
    # （そうでない「┌──」の行は出力。罫線の表などをプロンプトに吸い込まない）
    psp_seen = any(L.ev.get("psp") for L in lines if L.alt is None)
    last_in = -1
    i = 0
    while i < n:
        det = starts.get(i)
        if det is None:
            (cur["lines"] if cur is not None else pre).append(lines[i])
            i += 1
            continue
        _, j, d, foreign = det
        label, prompt, cmd, rp_exit, extra, info = d
        if info.get("dangle"):
            # 改行で終わらなかった前のコマンドの出力（bash などでプロンプトと同じ行に続いたもの）
            (cur["lines"] if cur is not None else pre).append(_text_line(info["dangle"], lines[i]))
        if info.get("ps2") and cur is not None and cur["kind"] == "cmd" \
                and _only_blank(lines, last_in, i):
            # 継続行（for> / dquote> / > など）: 直前のコマンドの続き（最後にまとめてつなぐ）
            if cmd:
                cur.setdefault("_more", []).append(cmd)
            if foreign:  # 継続行の入力受付中に割り込んだ出力（コマンドより前に置く）
                blocks.append(_misc_block(foreign, "misc"))
            cur.setdefault("_rows", []).extend(lines[i:j + 1])
            if extra is not None:
                cur["lines"].append(extra)
            last_in = j
            i = j + 1
            continue
        prev = cur if cur is not None else (blocks[-1] if blocks else None)
        if prev is not None and prev["exit"] is None and prev["kind"] == "cmd":
            W = info.get("W")
            if rp_exit is not None:  # 右プロンプトの終了コードは直前のコマンドのもの
                prev["exit"] = rp_exit
            elif shows_status and (info.get("rpw") or (
                    W and not info.get("stale") and info.get("used") is not None
                    and info["used"] + rp_max + 2 <= W)):
                # 失敗なら右プロンプトに ✘ が出るはずの余白があるのに出ていない → 成功
                # （入力が長くて右プロンプトが消された可能性があるときは不明のままにする）
                prev["exit"] = 0
                prev["xi"] = True
        if cur is not None:
            blocks.append(cur)
        holder = blocks[-1]["lines"] if blocks else pre
        take = 0 if info.get("col0") else _top_take(holder, psp_seen)
        top = holder[-1] if take else None
        if take:  # 2 行プロンプトの上段はプロンプト側へ（間の空行・PROMPT_SP の行は捨てる）
            prompt = holder[-1].text.strip("\n") + "\n" + prompt
            del holder[-take:]
        if foreign:
            blocks.append(_misc_block(foreign, "misc"))
        Lj = lines[j]
        t = Lj.t1 if Lj.t1 is not None else Lj.t0  # 行末の改行 = Enter を押した時刻
        cur = new_block("cmd", cmd, prompt, t, label)
        # プロンプトと入力が書かれていた画面の行（HTML で端末と同じ色を付けるのに使う）
        cur["_rows"] = ([top] if top is not None else []) + lines[i:j + 1]
        if extra is not None:
            cur["lines"].append(extra)
        last_in = j
        i = j + 1
    if cur is not None:
        blocks.append(cur)
    for b in blocks:
        more = b.pop("_more", None)
        if more:
            b["cmd"] = "\n".join(([b["cmd"]] if b["cmd"] else []) + more)
    # 空 Enter だけのプロンプト（出力も無い）は捨てる
    blocks = [b for b in blocks if b["cmd"] or _has_content(b["lines"])]
    return pre, blocks


def _norm_ws(s):
    return " ".join(s.split())


def _prompt_rx(prompt):
    """プロンプト → 描き直された同じプロンプトに合う正規表現（数字は変わってよい）。描き直した行は折り返さずに
    読むので、端末幅で折り返していたプロンプトは段の境目に空白があってもなくてもよいものとして全体で合わせ、
    それで合わなければ最後の段だけで合わせる。"""
    def rx(p):
        return "".join(r"\d+" if x.isdigit() else re.escape(x) for x in re.split(r"(\d+)", p) if x)
    parts = [p for p in (_norm_ws(x) for x in prompt.split("\n")) if p]
    if not parts:
        return None
    alts = [r"\s*".join(rx(p) for p in parts)] + ([rx(parts[-1])] if len(parts) > 1 else [])
    return re.compile("|".join("(?:%s)" % a for a in alts))


def _trim_common(a, b):
    """a から b と共通の先頭部分を除いた残り（打ちかけの入力の後ろに割り込んだ出力を取り出す）。
    空白の数の違いは同じとみなし、残りは a のまま返す（割り込んだ出力の空白を詰めない）。"""
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i].isspace() and b[j].isspace():
            while i < len(a) and a[i].isspace():
                i += 1
            while j < len(b) and b[j].isspace():
                j += 1
        elif a[i] == b[j]:
            i += 1
            j += 1
        else:
            break
    return a[i:].strip()


def _leftovers(lines, refs=()):
    """コマンドにならなかったプロンプト（空 Enter・Ctrl-C・入力受付中の背景ジョブの出力）を
    余り行として残すときの形。何も打たれていないプロンプトと、プロンプトの上段（A の印から入力行までの
    行。枠の無い 2 行プロンプトも）は捨て、入力確定の印が無い行（背景ジョブの出力が入力位置に
    出たもの）はその出力だけを残す。Ctrl-C で捨てた入力は、プロンプトの下段ごと残す。"""
    out = []
    tops = 0  # 直前に積んだプロンプトの上段の数（入力行が来たら取り除く）
    for L in lines:
        starts = any(mk[0] == "A" for mk in L.marks)
        if starts:
            tops = 0
        if _blank(L):
            continue
        if L.alt is None and ("in" in L.ev or "b" in L.ev):
            if tops:
                del out[-tops:]
            tops = 0
            d = input_split(L)
            if not d[2]:
                if out and out[-1].alt is None and _PROMPT_TOP.match(out[-1].text):
                    out.pop()
                continue
            if d[5]["noacc"]:
                # 入力行の右に割り込んだ出力。それより前に打っていた入力（実行されていない）は、Ctrl-C で捨てた
                # 入力と同じくプロンプトごと残す（同じジョブの他の出力の行と形を比べて分ける）
                kx = _typed_end(L, d[2], [x.text for x in lines if x is not L and not _blank(x)] + list(refs))
                if kx:
                    out.append(_text_line(d[1].rsplit("\n", 1)[-1].rstrip() + " " + d[2][:kx].strip(), L))
                L = _text_line(d[2][kx or 0:], L)
            out.append(L)
            continue
        out.append(L)
        if starts or tops:
            tops += 1  # A の印のある行から入力行までは、プロンプト（の上段）
    return out


def _split_region(region, a_at, ecmd=None, refs=()):
    """シェル統合ありのとき、C 直前の「プロンプト領域」を分ける。

    ecmd: OSC 633;E で受け取ったコマンド文字列（zsh）。無ければ入力行から読む（bash）。
    refs: これまでの余り行の文字列（入力行の右に割り込んだ背景ジョブの出力と、打ちかけの入力を分けるのに使う）
    戻り値: (余り行, プロンプト, 表示名, 入力から読んだコマンド, 出力行 or None, 貼り付けの続き,
            プロンプトと入力が書かれていた行（入力を描き直した行も。色を拾うのに使う）)
      余り行 = A より前に出た行と、入力に属さない行（入力受付中に来た背景ジョブの出力など）
    """
    s0 = a_at if a_at is not None and a_at <= len(region) else 0
    misc = _leftovers(region[:s0], refs)
    body = region[s0:]
    nb = [x for x, L in enumerate(body) if not _blank(L)]
    if not nb:
        return misc, "", "shell", "", None, [], []
    marked = [x for x, L in enumerate(body) if L.alt is None and ("in" in L.ev or "b" in L.ev)]
    bs = [x for x in marked if "b" in body[x].ev]
    # bash（B の印あり）は最後に描き直したプロンプト（Ctrl-L 等）、zsh は最初の入力行（以降は継続行）
    f = bs[-1] if bs else (marked[0] if marked else None)
    if bs:
        # それより前の行は、直前の 2 行プロンプトの上段を除いて入力とは関係ない（描き直す前の画面）
        keep = f - 1 if f > 0 and _PROMPT_TOP.match(body[f - 1].text) else f
        misc.extend(_leftovers(body[:keep], refs))
        body, nb = body[keep:], [x - keep for x in nb if x >= keep]
        marked = [x - keep for x in marked if x >= keep]
        f -= keep
    extra = None
    if f is None:
        # 入力受付の印が無い（readline を使わない入力等）: 最後の空でない行が入力行
        g = nb[-1]
        r = _regex_det(body[g], pad=True, rprompt=False)
        label, p, c = (r[0], r[1], r[2]) if r else ("shell", "", body[g].text.strip())
        prompt = "\n".join([body[x].text for x in nb[:-1]] + ([p.rstrip()] if p else []))
        return misc, prompt, label, c, None, [], [body[x] for x in nb]
    label, p, c, _, extra, info = input_split(body[f])
    last = f
    if info["top"]:
        # 印が行頭（bash）で、その行は 2 行プロンプトの上段: 入力は次の空でない行
        g = next((x for x in nb if x > f), None)
        if g is not None and "in" not in body[g].ev:
            r = _regex_det(body[g], pad=True, rprompt=False)
            p = body[f].text.rstrip() + "\n" + (r[1].rstrip() if r else "")
            c = r[2] if r else body[g].text.strip()
            last = g
    head = [body[x].text for x in nb if x < f]
    prompt = "\n".join(head + ([p] if p else []))
    typed = [c] if c else []
    cont = [x for x in marked if x > last]
    cset = set(cont)
    # 継続行（PS2）は入力の一部
    cont_noacc = []  # 入力受付中に別の出力が割り込んだ継続行（その行の文字列）
    for x in cont:
        d2 = input_split(body[x])
        if d2[2]:
            typed.append(d2[2])
            if d2[5]["noacc"]:
                cont_noacc.append((x, d2[2]))
        if d2[4] is not None:
            extra = d2[4]
    oth = [x for x in nb if x > last and x not in cset]
    acc_after = any(x not in cset and _accepts_here(body[x]) for x in range(last + 1, len(body)))
    cut = []  # (行の番号, 行): 入力行・継続行の右に割り込んだ出力（後の行と合わせて画面の順に余り行にする）
    pend = []
    redraw = set()  # 入力を描き直した行（色はここから拾う。端末に最後に残ったのはこの色）
    if ecmd is not None:
        e = _norm_ws(ecmd)
        # 入力の再表示とみなすのは、E 全体か E のどれかの行と同じ文字列だけ（部分一致にすると、
        # たまたまコマンドに含まれる短い出力（IP アドレス・ユーザ名など）を捨ててしまう）。
        # ジョブ通知などの後に zle が描き直した「プロンプト + 入力」の行も再表示
        elines = [_norm_ws(x) for x in ecmd.split("\n")]
        echoes = {e} | set(elines)
        cn = _norm_ws(c)
        rest_rows = set()  # 背景ジョブの出力の後に打ち足した入力の続き（確定行）

        def typed_cut(t, x0, x1, lines_):
            """入力行（継続行）の文字列 t のうち、打ちかけの入力を除いた残り（割り込んだ出力）。打ち足した続きの
            確定行（x0 より後・x1 より前で最初の確定行）と E の行を突き合わせて決める（出力の頭がたまたま E と
            同じ文字で始まっても削らない）。決められなければ None。"""
            ax = next((x for x in oth if x0 < x < x1 and _accepts_here(body[x])), None)
            if ax is None:
                return None
            t2 = _norm_ws(_acc_text(body[ax]))
            best = None
            for ln in lines_:
                need = _split_tail(ln, t2) if t2 else ln
                if need is None:
                    continue
                for nd in {need, need.rstrip()}:
                    r = _strip_head(t, nd)
                    if r is not None and (best is None or len(nd) > len(best[1])):
                        best = (r.strip(), nd, ln)
            if best is None:
                return None
            m = body[ax].ev.get("lclamp")
            if t2 and not best[1] and m:
                # 確定行に入力全体がある: zle が入力の頭へ戻って描き直した（オートサジェストの確定など）。戻ろうと
                # した桁数が、出力より前に打っていた部分
                r = _strip_head(t, best[2][:m])
                if r is not None:
                    best = (r.strip(), best[2][:m], best[2])
            if t2 and best[1].strip():
                rest_rows.add(ax)
            return best[0]

        first_cont = cont[0] if cont else len(body)
        if "acc" in body[f].ev and not info["col0"] and "b" not in body[f].ev and not info["stale"] \
                and not body[f].ev.get("up") and c and cn != elines[0]:
            # 改行で終わらなかった背景ジョブの出力の後に、同じ行で打った入力: 入力より左は出力
            x0 = _split_tail(c, elines[0])
            if x0 is not None and x0.strip():
                cut.append((f, _text_line(x0.strip(), body[f])))
        if info["noacc"] and _zle_free(body[f]) and acc_after and c and cn not in echoes \
                and not e.startswith(cn):
            # 入力行の右に出ていたのは背景ジョブの出力。本当の入力は ESC[?2004l のある行（E と同じ。打ち終えて
            # から Enter だけ押したなら空の行）。打ちかけの入力（E の先頭と同じ部分）は除いて、割り込んだ出力だけを
            # 残す（zle が自分で次の行へ移ったのなら、入力行の右は入力: 複数行の入力・補完の一覧・通知の前など）
            rest = typed_cut(c, last, first_cont, elines[:1])
            if rest is None:
                rest = _trim_common(c, e)
            if rest:
                cut.append((f, _text_line(rest, body[f])))
        for x, t in cont_noacc:
            # 継続行に割り込んだ出力も同じ（打ちかけの部分は E のどれかの行の先頭と同じ）
            if _zle_free(body[x]) and _norm_ws(t) not in echoes:
                nxc = next((y for y in cont if y > x), len(body))
                rest = typed_cut(t, x, nxc, elines)
                if rest is None:
                    rest = min((_trim_common(t, ln.strip()) for ln in ecmd.split("\n")), key=len)
                if rest:
                    cut.append((x, _text_line(rest, body[x])))
        # 複数行の入力（zle が自分で次の行へ移って描いた続きの行）: 何行目か
        chain = {}
        x, q = f, 0
        while x + 1 < len(body) and _zle_nl(body[x]) and not body[x].ev.get("up"):
            x += 1
            q += 1
            chain[x] = q
        prx = _prompt_rx(prompt)
        keep = []
        for x in oth:
            L = body[x]
            acc = _accepts_here(L)
            if acc:
                # 入力を確定した行。補完の一覧などの後なら、入力の続きはこの行に描き直されている
                redraw.add(x)
            t = _norm_ws(L.text)
            q = chain.get(x)
            if q is not None and q < len(elines) and elines[q] and t != elines[q] and _out_nl(L):
                # 複数行の入力の続きの行の右に、背景ジョブの出力が入った
                r = _strip_head(L.text.strip(), elines[q])
                if r and r.strip():
                    cut.append((x, _text_line(r.strip(), L)))
                    redraw.add(x)
                    continue
            if acc and t != e and t.endswith(e):
                # 「プロンプト + 入力」を描き直した行か、改行で終わらなかった背景ジョブの出力の後に打った入力
                x0 = _split_tail(L.text.strip(), e)
                ab = L.ev.get("above")
                # zle がカーソルを上へ戻して入力行を描き直すと、この行（補完の一覧の最後の行など）に重なる: 重なる
                # 前の段を使う（実際の画面では別の行）。zle の状態行（履歴検索など）は確定の前に zle が消している
                x1 = "\n".join("".join(t for t, _ in row) for row in ab) if ab else x0
                if x0 is not None and x1.strip() and not _PROMPT_END.search(x1) and not _ZLE_STATUS.match(x1.strip()) \
                        and not (prx is not None and prx.match(_norm_ws(x1))):
                    cut.append((x, _rows_line(L, ab) if ab else _text_line(x1.strip(), L)))
            # 確定行に打ち足した入力の続き（打ちかけ + 背景ジョブの出力 + 続き、で E になる）も入力の再表示
            if t and (t in echoes or x in rest_rows or (acc and t.endswith(e))):
                redraw.add(x)
            else:
                keep.append(x)
        # 2 行プロンプトを描き直したときの上段（描き直した入力行の直前にある、元の上段と同じ形の行）も出力ではない
        trx = _prompt_rx(head[-1]) if head else None
        if trx is not None:
            nxt = {a: b for a, b in zip(nb, nb[1:])}
            for x in list(keep):
                if nxt.get(x) in redraw and trx.match(_norm_ws(body[x].text)):
                    keep.remove(x)
                    redraw.add(x)
        oth = keep
    elif bs:
        # bash で複数行を貼り付けたとき、2 行目以降の入力はここに並ぶ（それぞれに C が来る）。
        # readline が貼り付けを描き直した行（同じ文字列）は 1 回だけにする
        seen_t = {_norm_ws(c)}
        for x in oth:
            t = _norm_ws(body[x].text)
            if t and t not in seen_t:
                seen_t.add(t)
                pend.append(body[x])
        oth = []
    elif info["noacc"] and c and _out_nl(body[f]) and _zle_free(body[f]) and not acc_after \
            and not any(body[x].ev.get("up") or _zle_nl(body[x]) for x in range(last + 1, len(body))):
        # 確定されないまま終わった入力行（ログの終わり）で、入力行の右の後の改行が出力のもの: 入力行の右と後の
        # 行は、背景ジョブが出し続けた出力（zle がプロンプトを描き直していれば、入力行の右は打ちかけの入力。
        # 描き直した行に同じものがある）
        prx = _prompt_rx(p)
        if prx is None or not any(prx.match(_norm_ws(body[x].text)) for x in oth):
            # 出力より前に打っていた入力（実行されていない）は、Ctrl-C で捨てた入力と同じくプロンプトごと残す
            kx = _typed_end(body[f], c, [body[x].text for x in oth] + list(refs))
            if kx:
                cut.append((f, _text_line(p.rsplit("\n", 1)[-1].rstrip() + " " + c[:kx].strip(), body[f])))
                c = c[kx:]
            cut.append((f, _text_line(c, body[f])))
    nbs = set(nb)
    pos = [x for x, _ in cut] + oth
    if pos:
        # 割り込んだ出力の行の間にある空行（出力の改行で終わったもの）も出力
        lo, hi = min(pos), max(pos)
        oth = oth + [x for x in range(lo + 1, hi) if x not in nbs and body[x].alt is None and _out_nl(body[x])
                     and x not in redraw and x not in cset]
    misc.extend(L for _, L in sorted(cut + [(x, body[x]) for x in oth], key=lambda t: t[0]))
    used = [L for x, L in enumerate(body) if (x <= last and x in nbs) or x in cset or x in redraw]
    return misc, prompt, label, "\n".join(typed), extra, pend, used


def _infer_blocks(lines, label):
    """シェル統合の印が無い区間（統合の無いシェルで打った分・入れ子のシェルを exit した後など）を、
    入力受付の印（ESC[?2004h）から推定して区切る。その印も無ければ 1 つの余りブロックにする
    （出力の中のプロンプトらしい文字列からは、コマンドを作らない）。"""
    if not _has_content(lines):
        return []
    if not any(L.alt is None and ("in" in L.ev or "b" in L.ev) for L in lines):
        return [_misc_block(lines, label)]

    def detect(L):
        return input_split(L) if ("in" in L.ev or "b" in L.ev) else None

    pre, bl = segment_lines(lines, _plan(_detections(lines, detect, True)))
    return ([_misc_block(pre, label)] if _has_content(pre) else []) + bl


def segment_marked(lines, tmap, nonces=None):
    """シェル統合の印（本物の合言葉付きのものだけが残っている）で区切る。

    印の合言葉はシェルごとに違う。実行中のコマンドの中では、そのコマンドを起動したシェルの印だけを
    受け付ける（ネストしたシェルや ssh 先のシェル、古いログの cat などの印はただの出力として扱い、
    _nest でリモート側のプロンプトとして分ける）。別のシェルに引き継ぐのは、今のシェルがこの後
    もう新しい印（番号の進んだ印）を出さない場合で、相手が親子関係のシェル（exec・入れ子から戻った・
    入れ子の中でロギングを始めた）か、後から起動したシェル（respawn 等）のときだけ。
    nonces: {合言葉: {"order": 台帳での順番, "parent": 親の合言葉}}
    """
    tof = (lambda off: tmap.time_of(off)) if tmap else (lambda off: None)
    reg = nonces or {}

    def info_of(n):
        v = reg.get(n)
        return v if isinstance(v, dict) else {}

    def related(a, b):
        # a と b が祖先・子孫の関係か（親の合言葉をたどる）
        for x, y in ((a, b), (b, a)):
            seen = set()
            p = info_of(y).get("parent")
            while p is not None and p not in seen:
                if p == x:
                    return True
                seen.add(p)
                p = info_of(p).get("parent")
        return False

    # シェルごとの印の位置と、そこから後ろで最大のコマンド番号（「まだ動いているか」を調べる）
    pos_of = {}
    dend = {}  # (シェル, 番号) → その番号の D が最後に現れる位置
    for L in lines:
        for mk in L.marks:
            pos_of.setdefault(mk[3], []).append((mk[2], -1 if mk[4] is None else mk[4]))
            if mk[0] == "D" and mk[4] is not None:
                dend[(mk[3], mk[4])] = mk[2]
    after = {}
    for n, lst in pos_of.items():
        best, sm = -1, []
        for _, k in reversed(lst):
            best = max(best, k)
            sm.append(best)
        sm.reverse()
        after[n] = ([o for o, _ in lst], sm)

    def alive(n, ref, off, running):
        # シェル n が off より後ろで新しく動くか: 実行中ならそのコマンドの D か番号の進んだ印、
        # 待機中なら番号が ref 以上の印（古い番号の印はログの再生なので数えない）
        ol, sm = after.get(n, ((), ()))
        i = bisect.bisect_right(ol, off)
        if i >= len(ol):
            return False
        ref = -1 if ref is None else ref
        if running:
            return sm[i] > ref or dend.get((n, ref), -1) > off
        return sm[i] >= ref

    seen = {}  # シェルごとに、これまでに現れた最大の番号（印を受け付けたかどうかに関係なく）

    def may_switch(new, old, ref, off, running, kk):
        if alive(old, ref, off, running):
            return False
        if kk is not None and kk <= seen.get(new, -1):
            return False  # 以前に見た番号の印 = そのシェルの過去の印の再生（cat / tail など）
        return related(new, old) or info_of(new).get("order", 0) > info_of(old).get("order", 0)

    blocks = []
    region = []
    a_at = None
    cur = None
    cur_n = cur_k = None      # 実行中のコマンドを起動したシェルの合言葉と、そのコマンドの番号
    shell_n = shell_k = None  # 今プロンプトを出しているシェルの合言葉と、最後に始まったコマンドの番号
    pend_e = None
    pending = []              # bash で貼り付けた 2 行目以降の入力（次の C のコマンド文字列になる）
    cwd = None
    first = True

    bg_seen = []  # これまでの余り行（入力受付中に割り込んだ背景ジョブの出力など。同じジョブの出力と形を比べる）

    def emit_misc(misc):
        nonlocal first
        if _has_content(misc):
            blocks.append(_misc_block(misc, "start" if first else "misc"))
            bg_seen.extend(L.text for L in misc if not _blank(L))
            del bg_seen[:-40]
        first = False

    no_d = set()  # D を出さずに次の C を出したことのあるシェル（PROMPT_COMMAND が上書きされた bash 等）

    def flush_dead():
        # 今のプロンプト領域の後始末（ログの終わり・別のシェルへの引き継ぎ）。入力途中のプロンプトと入力は
        # 捨てて残りは余りにする。ただし、そのプロンプトの後ろにまだ入力行がある（Ctrl-D でシェルを抜けて
        # 外側の統合の無いシェルが続いた）なら、そこから後ろは推定で区切る
        nonlocal region, a_at
        if not region:
            return
        s0 = a_at or 0
        ins = [x for x in range(s0, len(region))
               if region[x].alt is None and ("in" in region[x].ev or "b" in region[x].ev)]
        later = [x for x in ins[1:] if input_split(region[x])[2] and not input_split(region[x])[5]["ps2"]]
        if ins and later:
            cut = ins[0] + 1
            emit_misc(_split_region(region[:cut], a_at, refs=bg_seen)[0])
            blocks.extend(_infer_blocks(region[cut:], "misc"))
        else:
            emit_misc(_split_region(region, a_at, refs=bg_seen)[0])
        region, a_at = [], None

    def close(b):
        # exit / logout で終わったシェルの後ろは、その外側の（統合の無い）シェルの続き: 推定で区切る
        if b["running"] is False and b["exit"] is None and tool_of(b["cmd"]) in ("exit", "logout"):
            rest, b["lines"] = b["lines"], []
            blocks.append(b)
            blocks.extend(_infer_blocks(rest, "misc"))
        else:
            blocks.append(b)

    def older(kk, ref, strict):
        # 同じシェルの印のコマンド番号は戻らない。戻っていればログの再生（cat / tail など）
        if kk is None or ref is None:
            return False
        return kk <= ref if strict else kk < ref

    def accept(kind, off, nn, kk):
        nonlocal cur, cur_n, cur_k, shell_n, shell_k
        if cur is not None:
            if nn == cur_n:
                # 実行中: そのコマンドの D か、D が欠けたまま始まった次のコマンド（E / C）だけ
                if kind == "A" or (kind == "D" and kk is not None and cur_k is not None
                                   and kk != cur_k) or (kind in "CE" and older(kk, cur_k, True)):
                    return False
                return not (kind == "C" and kk is None and not _has_content(cur["lines"]))
            if not may_switch(nn, cur_n, cur_k, off, True, kk):
                return False
            # 実行中のコマンドのシェルがもう動かない（exec で置き換わった・終了した）
            cur["te"] = tof(off)
            close(cur)
            cur = None
            cur_n = cur_k = None
            shell_n, shell_k = nn, kk
            return True
        if shell_n is not None:
            if nn != shell_n:
                # 待機中に別のシェルの印（入れ子から戻った・respawn など）
                if not may_switch(nn, shell_n, shell_k, off, False, kk):
                    return False
                flush_dead()
                shell_n, shell_k = nn, kk
                return True
            return not older(kk, shell_k, kind in "CE")
        return True

    for L in lines:
        for kind, val, off, nn, kk in L.marks:
            ok = accept(kind, off, nn, kk)
            if kk is not None and kk > seen.get(nn, -1):
                seen[nn] = kk
            if not ok:
                continue
            if kind == "E":
                pend_e = val
            elif kind == "A":
                if cur is None:
                    shell_n = nn
                    if kk is not None:
                        shell_k = kk
                    a_at = len(region)
                    if val is not None:
                        cwd = val
                    if pending:  # 使われなかった貼り付けの残り
                        blocks.append(_misc_block(pending, "misc"))
                        pending = []
            elif kind == "C":
                if cur is not None:
                    # D が来ないまま次の C（bash の PROMPT_COMMAND が上書きされた・複数行の貼り付け等）:
                    # 前のブロックの最後の入力行から後ろが、このコマンドのプロンプト領域
                    k = next((x for x in range(len(cur["lines"]) - 1, -1, -1)
                              if cur["lines"][x].alt is None
                              and ("in" in cur["lines"][x].ev or "b" in cur["lines"][x].ev)), None)
                    if k is not None:
                        if k > 0 and _PROMPT_TOP.match(cur["lines"][k - 1].text):
                            k -= 1
                        region, a_at = cur["lines"][k:] + region, 0
                        del cur["lines"][k:]
                    if cur_n == nn:
                        no_d.add(nn)
                    close(cur)
                    cur = None
                ecmd = pend_e.decode("utf-8", errors="replace") if pend_e is not None else None
                if ecmd is not None and not ecmd.strip():
                    ecmd = None  # 履歴に残らない入力などで E が空: 入力行から読む
                if first and a_at:
                    # 最初の印より前（統合の無いシェルで打った分・ロギング開始時の取り込み）は推定で区切る
                    blocks.extend(_infer_blocks(region[:a_at], "start"))
                    region, a_at, first = region[a_at:], 0, False
                elif first and a_at is None and region:
                    # A が無いまま最初の C（プロンプトを出した後でロギングを始めた）: 取り込んだ画面は
                    # 「開始時の画面」、最後の入力行（とプロンプトの行）だけをこのコマンドのものにする
                    ins = [x for x, R in enumerate(region)
                           if R.alt is None and ("in" in R.ev or "b" in R.ev)]
                    nbr = [x for x, R in enumerate(region) if not _blank(R)]
                    k = ins[-1] if ins else (nbr[-2] if len(nbr) >= 2 else (nbr[-1] if nbr else 0))
                    if ins and k > 0 and _PROMPT_TOP.match(region[k - 1].text):
                        k -= 1
                    if k > 0:
                        blocks.extend(_infer_blocks(region[:k], "start"))
                        region, first = region[k:], False
                    a_at = 0
                misc, prompt, label, c, extra, pend, used = _split_region(region, a_at, ecmd, refs=bg_seen)
                emit_misc(misc)
                if not c and ecmd is None and pending:
                    c = pending.pop(0).text.strip()  # 貼り付けた次の行
                pending.extend(pend)
                cur = new_block("cmd", (ecmd if ecmd is not None else c).strip(), prompt,
                                tof(off), label)
                cur["exact"] = True
                cur["_rows"] = used  # プロンプトと入力が書かれていた画面の行（HTML で端末と同じ色を付ける）
                if ecmd is None:
                    cur["screen"] = True  # コマンド文字列は画面（入力行）から読んだもの（bash 等）
                cur_n, cur_k = nn, kk
                shell_n, shell_k = nn, kk
                if extra is not None:
                    cur["lines"].append(extra)
                if cwd is not None:
                    cur["cwd"] = cwd.decode("utf-8", errors="replace")
                region = []
                a_at = None
                pend_e = None
            elif kind == "D":
                if cur is not None:
                    cur["exit"] = val
                    cur["te"] = tof(off)
                    blocks.append(cur)
                    cur = None
                    cur_n = cur_k = None
        if cur is not None:
            cur["lines"].append(L)
        else:
            region.append(L)
    if cur is not None:
        if tool_of(cur["cmd"]) in ("exit", "logout"):
            close(cur)  # シェルを抜けた後（外側の統合の無いシェル）
        else:
            k = None
            if cur_n in no_d:
                # D を出さなくなったシェル: 最後の入力行（次のプロンプト）があればコマンドは終わっている
                k = next((x for x in range(len(cur["lines"]) - 1, 0, -1)
                          if cur["lines"][x].alt is None
                          and ("in" in cur["lines"][x].ev or "b" in cur["lines"][x].ev)), None)
            if k is not None:
                if k > 0 and _PROMPT_TOP.match(cur["lines"][k - 1].text):
                    k -= 1
                region, a_at = cur["lines"][k:], 0
                del cur["lines"][k:]
                blocks.append(cur)
                flush_dead()
            else:
                cur["running"] = True
                blocks.append(cur)
    elif region:
        flush_dead()
    return blocks


def _nest(block):
    """対話ツールの出力の中を、リモート側のプロンプトで子ブロックに分ける。"""
    lines = block["lines"]
    if not lines:
        return
    tool = tool_of(block["cmd"])
    if not is_interactive(tool):
        # 対話ツール以外（curl / cat など）の出力は、プロンプトらしい行や ESC[?2004h が混ざっていても
        # 分けない（出力から偽のコマンドを作らせない）
        return
    marked = any("in" in L.ev or "b" in L.ev for L in lines)

    def detect(L):
        if "in" in L.ev or "b" in L.ev:
            label, p, c, ex, extra, info = input_split(L, REMOTE_PROMPTS + REPL_PROMPTS, tool)
            return (label if label != "shell" else (tool or "shell")), p, c, ex, extra, info
        if marked:
            # 入力受付の印を出すツールでは、印の無い行（= 出力）をプロンプトとみなさない
            # （出力に「PS C:\> ...」のような文字列があっても偽のコマンドにしない）
            return None
        return _regex_det(L, REMOTE_PROMPTS, tool)

    pre, kids = segment_lines(lines, _detections(lines, detect, marked, instant=False))
    if not kids:
        return
    block["lines"] = pre
    for k in kids:
        k["kind"] = "remote"
        k["remote"] = k.pop("label") or tool
        k["label"] = None
    block["children"] = kids


def _finish(b):
    lines = b["lines"]
    i = 0
    while i < len(lines) and _blank(lines[i]):
        i += 1
    del lines[:i]
    while lines and _blank(lines[-1]):
        lines.pop()
    if b["te"] is None:
        for L in reversed(lines):
            if L.t1 is not None:
                b["te"] = L.t1
                break
        else:
            b["te"] = b["t"]
    for k in b["children"]:
        _finish(k)
        if k["t"] is None:
            k["t"] = b["t"]
    if b["children"] and b["children"][-1]["te"] is not None and \
            (b["te"] is None or b["children"][-1]["te"] > b["te"]):
        b["te"] = b["children"][-1]["te"]


def build_blocks(lines, tmap=None, nonces=None):
    """戻り値: (ブロックのリスト, 方式 'exact' | 'readline' | 'regex')

    nonces: split_raw に渡したのと同じ {合言葉: 記録時刻}（exec の引き継ぎ判定に使う）
    """
    if any(mk[0] == "C" for L in lines for mk in L.marks):
        blocks = segment_marked(lines, tmap, nonces)
        mode = "exact"
    else:
        readline = any("in" in L.ev or "b" in L.ev for L in lines)
        if readline:
            # zle は入力受付の印をプロンプトの後ろに出す。PROMPT_SP の出ている（= zsh の）ペインで
            # 印が行頭にある行は、実行中の別のプログラム（gdb / php -a / python などの REPL）の入力
            zsh_pane = any(L.ev.get("psp") for L in lines if L.alt is None)

            def detect(L):
                if not ("in" in L.ev or "b" in L.ev):
                    return None
                d = input_split(L)
                if zsh_pane and d[5]["col0"] and "b" not in L.ev:
                    return None
                return d
            mode = "readline"
        else:
            def detect(L):
                return _regex_det(L)
            mode = "regex"
        pre, blocks = segment_lines(lines, _plan(_detections(lines, detect, readline)))
        if _has_content(pre):
            blocks.insert(0, _misc_block(pre, "start"))
    for b in blocks:
        if b["kind"] == "cmd":
            _nest(b)
        _finish(b)
    return blocks, mode
