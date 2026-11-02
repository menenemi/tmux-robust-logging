#!/usr/bin/env perl
# バイトセーフな端末出力サニタイザ。エンコーディングを仮定しない。
# - OSC/DCS/SOS/PM/APC/CSI/2バイトESC を除去（EOF で切れた未終端シーケンスも処理）
# - CRLF -> LF に正規化
# - 行内の CR / BS / 行内編集 CSI（カーソル移動・消去・削除・挿入）を
#   「小さな端末」としてエミュレートし、上書き後に実際に見えていた行を再構成する。
#   zle が入力の折返し時に使う縦移動 (CUU/CUD) は行内の複数行として再現する。
#   BS はバイト削除ではなくカーソル移動として扱うため、UTF-8 のマルチバイト文字を
#   backspace で消しても文字化けしない（全角 2 桁幅対応）。
use strict;
use warnings;
use POSIX qw(strftime);

binmode STDIN;
binmode STDOUT;

local $/;
my $d = <STDIN>;
$d = '' unless defined $d;

# タイムスタンプ付与（任意）: RL_TSINDEX が指す sidecar があれば、raw の元バイト位置と
# 時刻の対応から、出力の各論理行の先頭に日時を付ける。環境変数が無ければ従来と完全に同一。
# ここで $d はまだ raw の元バイトそのもの（変換前）なので、オフセットが sidecar と一致する。
our @LINETIME;   # 論理行 i に対応する時刻文字列
our $TS_ON = 0;
our $LINE_IDX = 0;
if (defined $ENV{RL_TSINDEX} && length $ENV{RL_TSINDEX} && -f $ENV{RL_TSINDEX}) {
  _build_linetime($d, $ENV{RL_TSINDEX}, $ENV{RL_TS_FORMAT});
  $TS_ON = 1 if @LINETIME;
}

# Windows 由来の CRLF は先に LF へ正規化する
# （「未終端シーケンスは改行で打ち切る」ガードが \r\n 直前でも効くように）
$d =~ s/\r+\n/\n/g;

# DCS/SOS/PM/APC: ESC P/X/^/_ ... ST。未終端は改行で打ち切る
# （未終端 DCS が後続ログを丸ごと飲み込むのを防ぐ）。
# tmux passthrough (ESC Ptmux; ...) は内部の ESC を二重化して埋め込むため、
# ESC ESC のペアは本文として読み飛ばす（中の編集 CSI を実行してしまわないように）。
# 重要: この規則は OSC 除去より先に適用すること。後にすると、passthrough に
# 包まれた BEL 終端 OSC の内側へ OSC 規則がマッチして passthrough を食い破り、
# 残骸の DCS が改行までの正当なテキストを飲み込んでしまう。
$d =~ s/\x1b[PX^_](?:\x1b\x1b|[^\x1b\n])*(?:\x1b\\|(?=\n))//g;
$d =~ s/\x1b[PX^_](?:\x1b\x1b|[^\x1b\n])*\z//;        # EOF で未終端
# はぐれた二重 ESC は両方除去（後続バイト列を保持 CSI として実行させない）
$d =~ s/\x1b\x1b//g;
# OSC: ESC ] ... (BEL | ST)。ウィンドウタイトル設定などが該当。
# 未終端のまま改行が来たら壊れたシーケンスとして打ち切る（改行自体は残す）。
$d =~ s/\x1b\][^\x07\x1b\n]*(?:\x07|\x1b\\|(?=\n))//g;
$d =~ s/\x1b\][^\x07\x1b\n]*\z//;                     # EOF で未終端

# CSI: 行内編集に関わるもの（@ A B C D G J K P X・数字パラメータのみ）は残し、
# 後段のレンダラでカーソル移動・消去として解釈する。zsh-autosuggestions 等が
# カーソル移動で戻って再描画・消去するのを単純削除すると、コマンドの二重記録や
# 「折返し行の消去がプロンプト行を消す」事故になるため。色 SGR・モード切替などは除去。
$d =~ s/\x1b\[(?![0-9;]*[\@ABCDGJKPX])[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]//g;
$d =~ s/\x1b\[[\x30-\x3f]*[\x20-\x2f]*(?=\n)//g;      # 改行で未終端（パラメータを漏らさない）
$d =~ s/\x1b\[[\x30-\x3f]*[\x20-\x2f]*\z//s;          # EOF で未終端
# ESC + intermediate(s) + final（文字集合指定 ESC ( B など）
$d =~ s/\x1b[\x20-\x2f]+[\x30-\x7e]//g;
# 残りの 2 バイト ESC（ESC 7, ESC = など。ESC [ は保持 CSI の先頭なので除外）
$d =~ s/\x1b[\x30-\x5a\x5c-\x7e]//g;
# はぐれ ESC（保持した行内編集 CSI の先頭は残す）
$d =~ s/\x1b(?!\[[0-9;]*[\@ABCDGJKPX])//g;

# 各行を端末としてレンダリング（タイムスタンプ ON なら各論理行の先頭に付与）。
# \n の数はここまでの変換で不変なので、論理行 i は元 raw の論理行 i に 1:1 対応する。
$d =~ s{^([^\n]*)}{ _render_line_ts($1) }gme;

# 残った C0 制御文字を除去（TAB と LF は残す。NUL は UTF-16 混入対策で必ず落とす）
$d =~ s/[\x00-\x08\x0b-\x1f\x7f]//g;

print $d;

# ---- 行レンダラ ------------------------------------------------------------
# 論理行（\n 区切り）を小さな端末として再生する。@rows の各要素が画面上の 1 行。
# セル配列: 1 要素 = 1 桁。ワイド文字は先頭桁に文字全体、右桁にマーカー('')。
# CR = 桁 0、BS = 1 桁左、CUU/CUD = 行間移動（論理行の先頭でクランプ）、他は上書き。
# UTF-8 として読めないバイトは 1 バイト = 1 桁で扱う（CP932 等の生バイトを壊さない）。

sub _render_line {
    my ($line) = @_;
    return $line unless $line =~ /[\x08\x0d\x1b]/; # 高速路: CR/BS/CSI のない行はそのまま
    my @rows = ([]);
    my $r    = 0;
    my $c    = $rows[0]; # 現在行のセル配列（リファレンス）
    my $cur  = 0;
    # 桁 $pos で「縦の切断」（消去・削除・挿入の境界）が起きるとき、
    # ワイド文字をまたいでいたら文字全体を空白化する（端末と同じ挙動）
    my $norm_cut = sub {
        my ($pos) = @_;
        if ($pos > 0 && defined $c->[$pos] && $c->[$pos] eq '') {
            $c->[$pos - 1] = ' ';
            $c->[$pos]     = ' ';
        }
    };
    while ($line =~ /\G(\x1b\[[0-9;]*[\@ABCDGJKPX]|[\xc2-\xdf][\x80-\xbf]|[\xe0-\xef][\x80-\xbf]{2}|[\xf0-\xf4][\x80-\xbf]{3}|.)/gcs) {
        my $t = $1;
        if ($t eq "\x0d") { $cur = 0;           next; }
        if ($t eq "\x08") { $cur-- if $cur > 0; next; } # 左端で停止（端末と同じ）
        # 行内編集 CSI のエミュレート
        if ($t =~ /^\x1b\[([0-9;]*)([\@ABCDGJKPX])\z/) {
            my ($ps, $f) = ($1, $2);
            my $n = ($ps =~ /^(\d+)/) ? $1 : -1; # -1 = パラメータ省略
            if ($f eq 'A') {        # CUU: n 行上へ（この論理行の先頭行でクランプ）
                my $k = ($n < 1) ? 1 : $n;
                $r = ($r > $k) ? $r - $k : 0;
                $c = ($rows[$r] //= []);
            } elsif ($f eq 'B') {   # CUD: n 行下へ
                my $k = ($n < 1) ? 1 : $n;
                $k = 500 if $k > 500; # 巨大パラメータで行を量産しない
                $r += $k;
                $r = 500 if $r > 500;
                $c = ($rows[$r] //= []);
            } elsif ($f eq 'D') {   # CUB: n 桁左へ
                my $k = ($n < 1) ? 1 : $n;
                $cur -= $k;
                $cur = 0 if $cur < 0;
            } elsif ($f eq 'C') {   # CUF: n 桁右へ（セルは書き換えない）
                my $k = ($n < 1) ? 1 : $n;
                $k = 10000 if $k > 10000;
                # 累積位置もクランプする（[10000C の繰り返しによる肥大化・OOM 防止）。
                # 実在する行の長さまでは正当な移動として許す（長い行を壊さない）。
                my $lim = (@$c > 10000) ? scalar(@$c) : 10000;
                $lim = $cur if $cur > $lim;
                $cur += $k;
                $cur = $lim if $cur > $lim;
            } elsif ($f eq 'G') {   # CHA: n 桁目へ（1 始まり）
                my $k = ($n < 1) ? 1 : $n;
                my $lim = (@$c > 10000) ? scalar(@$c) : 10000;
                $cur = $k - 1;
                $cur = $lim if $cur > $lim;
            } elsif ($f eq 'J') {   # ED: 0/省略 = カーソル以降を消去（行内の下行も落とす）
                my $mode = ($n < 0) ? 0 : $n;
                if ($mode == 0) {
                    $norm_cut->($cur);
                    splice(@$c, $cur) if $cur < @$c;
                    splice(@rows, $r + 1) if $r < $#rows;
                }
                # mode 1/2（上方向・全画面消去）は画面操作なのでログには適用しない
            } elsif ($f eq 'K') {   # EL: 行内消去 (0=右, 1=左, 2=全体)
                my $mode = ($n < 0) ? 0 : $n;
                if ($mode == 0) {
                    $norm_cut->($cur);
                    splice(@$c, $cur) if $cur < @$c;
                } elsif ($mode == 1) {
                    my $end = ($cur < $#$c) ? $cur : $#$c;
                    for my $i (0 .. $end) { $c->[$i] = ' '; }
                    $norm_cut->($end + 1);
                } elsif ($mode == 2) {
                    @$c = ();
                }
                # 範囲外パラメータ (3 以上) は実端末と同じく無視
            } elsif ($f eq 'X') {   # ECH: n 文字ぶん空白化（シフトなし）
                my $k = ($n < 1) ? 1 : $n;
                $norm_cut->($cur);
                $norm_cut->($cur + $k);
                for my $i ($cur .. $cur + $k - 1) { last if $i > $#$c; $c->[$i] = ' '; }
            } elsif ($f eq 'P') {   # DCH: n 文字削除（左詰め）
                my $k = ($n < 1) ? 1 : $n;
                $norm_cut->($cur);
                $norm_cut->($cur + $k);
                splice(@$c, $cur, $k) if $cur <= $#$c;
            } elsif ($f eq '@') {   # ICH: n 文字ぶん空白挿入（行の総長もキャップ）
                my $k = ($n < 1) ? 1 : $n;
                $k = 10000 if $k > 10000;
                if (@$c + $k > 20000) {
                    my $room = 20000 - @$c;
                    $k = ($room > 0) ? $room : 0;
                }
                $norm_cut->($cur);
                splice(@$c, $cur, 0, (' ') x $k) if $k > 0 && $cur <= $#$c;
            }
            next;
        }
        # カーソルを動かさない C0 (BEL, SO/SI, DEL, はぐれ ESC 等) はセルを消費させない。
        # セルにすると後続の BS 消去が 1 桁ずれる（どうせ後段の C0 除去で消える）。
        # TAB は従来通り 1 セルとして残す（タブストップは近似しない）。
        if (length($t) == 1 && $t =~ /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/) { next; }
        my $w = _cell_width($t);
        # 書き込み先がワイド文字の右半分 -> そのワイド文字の左半分を空白化
        if ($cur > 0 && defined $c->[$cur] && $c->[$cur] eq '') {
            $c->[$cur - 1] = ' ';
        }
        # 書き込み範囲の最終桁がワイド文字の先頭 -> 右半分が孤児になるので空白化
        my $last = $cur + $w - 1;
        if (defined $c->[$last] && $c->[$last] ne ''
            && defined $c->[$last + 1] && $c->[$last + 1] eq '') {
            $c->[$last + 1] = ' ';
        }
        $c->[$cur] = $t;
        $c->[$cur + 1] = '' if $w == 2;
        $cur += $w;
    }
    # 内容のある最後の行までを出力（移動だけで書かれなかった下行は出さない）
    my $lastrow = $#rows;
    $lastrow-- while $lastrow > 0 && !@{ $rows[$lastrow] // [] };
    my @out;
    for my $i (0 .. $lastrow) {
        my $t = join '', map { defined($_) ? $_ : ' ' } @{ $rows[$i] // [] };
        $t =~ s/ +\z//; # 消去で残った行末の空白は落とす
        push @out, $t;
    }
    return join("\n", @out);
}

# 論理行を 1 つレンダリングし、必要ならその先頭にタイムスタンプを付ける。
# 空行（プロンプト間のスペーサ等）には付けない（日時でログが埋め尽くされないように）。
# 複数行に展開される行（折返し再現）では先頭行だけに付ける。
sub _render_line_ts {
    my ($line) = @_;
    my $r = _render_line($line);
    my $i = $LINE_IDX++;
    if ($TS_ON && length $r && $i < @LINETIME) {
        $r = $LINETIME[$i] . $r;
    }
    return $r;
}

# sidecar (「epoch offset」の昇順レコード) と元 raw の \n 位置から、
# 各論理行の時刻文字列 @LINETIME を作る。両者ともオフセット昇順なので 1 パスで対応づく。
sub _build_linetime {
    my ($orig, $idxfile, $fmt) = @_;
    $fmt = '[%Y-%m-%d %H:%M:%S] ' unless defined $fmt && length $fmt;

    open(my $F, '<', $idxfile) or return;
    local $/ = "\n"; # スクリプト冒頭の local $/;（slurp）を行単位に戻す
    my (@toff, @ttime);
    while (my $ln = <$F>) {
        if ($ln =~ /^(\d+)\s+(\d+)/) { push @ttime, $1; push @toff, $2; }
    }
    close($F);
    return unless @toff;

    # 各論理行の「開始」バイトオフセット（前の行の \n の直後。先頭行は 0）。
    # 行の完成時刻(\n の到着)ではなく、行の先頭バイトが書かれた時刻で刻む。
    # こうするとプロンプト行は「プロンプトが描画された時刻」(= PS1 と一致)で記録され、
    # 入力に数秒かけても「遅れて」見えなくなる。出力行は先頭バイト≒出現時刻なので正確。
    my $len = length $orig;
    my @starts = (0);
    my $p = 0;
    while (1) {
        my $nl = index($orig, "\n", $p);
        last if $nl < 0;
        $p = $nl + 1;
        push @starts, $p;        # 次の論理行はこの \n の直後から始まる
        last if $p > $len;
    }

    # レコードのオフセットは「そのチャンク書き込み後の累積バイト数」。開始位置 S の
    # バイトを書いたのは「累積が S を初めて超えるチャンク」= offset > S の最初のレコード。
    # 時刻の再フォーマットはオフセットが変わる時だけ（strftime 呼び出しを節約）。
    my $nrec = scalar @toff;
    my $j = 0;
    my ($cache_epoch, $cache_str) = (undef, '');
    for my $s (@starts) {
        $j++ while ($j < $nrec && $toff[$j] <= $s); # S より後の最初のレコード
        my $ti = ($j < $nrec) ? $j : $nrec - 1;     # 全て S 以下なら最後のレコード
        my $ep = $ttime[$ti];
        if (!defined $cache_epoch || $ep != $cache_epoch) {
            $cache_epoch = $ep;
            $cache_str   = strftime($fmt, localtime($ep));
        }
        push @LINETIME, $cache_str;
    }
}

sub _cell_width {
    my ($t) = @_;
    return 1 if length($t) == 1;
    my @b = unpack 'C*', $t;
    my $cp;
    if    (@b == 2) { $cp = (($b[0] & 0x1f) << 6)  |  ($b[1] & 0x3f); }
    elsif (@b == 3) { $cp = (($b[0] & 0x0f) << 12) | (($b[1] & 0x3f) << 6)  |  ($b[2] & 0x3f); }
    else            { $cp = (($b[0] & 0x07) << 18) | (($b[1] & 0x3f) << 12)
                          | (($b[2] & 0x3f) << 6)  |  ($b[3] & 0x3f); }
    # East Asian Wide / Fullwidth の主要レンジ（近似。CJK・かな・ハングル・絵文字）
    return 2 if ($cp >= 0x1100  && $cp <= 0x115F)
             || ($cp >= 0x2E80  && $cp <= 0xA4CF)
             || ($cp >= 0xAC00  && $cp <= 0xD7A3)
             || ($cp >= 0xF900  && $cp <= 0xFAFF)
             || ($cp >= 0xFE30  && $cp <= 0xFE4F)
             || ($cp >= 0xFF00  && $cp <= 0xFF60)
             || ($cp >= 0xFFE0  && $cp <= 0xFFE6)
             || ($cp >= 0x1F300 && $cp <= 0x1FAFF)
             || ($cp >= 0x20000 && $cp <= 0x3FFFD);
    return 1;
}
