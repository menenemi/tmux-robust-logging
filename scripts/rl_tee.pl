#!/usr/bin/env perl
# pipe-pane の書き込み先。stdin を raw.log へ「バイト完全一致」で追記しつつ、
# 「いつ・raw の何バイト目まで書いたか」を別ファイル(tsindex)へ best-effort で記録する。
# これにより raw の完全性を一切損なわずに、後段の整形でタイムスタンプを復元できる
# （WinRM 等リモート側のプロンプトに日時が無くてもログに日時が入る）。
#
# 使い方: rl_tee.pl <raw.log> <tsindex>
# 設計上の不変条件: 各チャンクで「先に raw を書き切ってから」sidecar に記録する。
# よってプロセスが途中で死んでも raw は常に完結しており、失われるのは最後の
# sidecar 1 行だけ（raw ファースト原則）。sidecar が開けなくても raw 記録は続行する。
use strict;
use warnings;

my ($raw, $tsindex) = @ARGV;
defined $raw or exit 1;

open(my $R, '>>', $raw) or exit 1;
binmode $R;
# STDIN も必ずバイナリに固定する。PERL_UNICODE 環境変数や -C 起動で STDIN に
# :utf8 レイヤが付くと sysread が致命的エラーになり、raw を 1 バイトも書けずに
# 死ぬ（cat 経路からの重大なリグレッションになる）。
binmode STDIN;
# syswrite はシステムコール直行でバッファしないため tail -f も追随できる

my $off = -s $raw;               # 開始オフセット（backlog 取り込み分・再開分を含む）
$off = 0 unless defined $off;

my $T;
if (defined $tsindex && length $tsindex && open($T, '>>', $tsindex)) {
  binmode $T;
  # autoflush: live 整形が進行中の sidecar を読めるように
  my $prev = select($T); $| = 1; select($prev);
  print $T time(), ' ', $off, "\n"; # 初期レコード（backlog 末尾 = ロギング開始時刻）
} else {
  $T = undef;
}

my $buf;
while (1) {
  my $n = sysread(STDIN, $buf, 65536);
  last if !defined $n || $n == 0; # エラー/EOF いずれも終了（raw は完結済み）
  # raw を先に、部分書き込みに備えて必ず全量書き切る
  my $w = 0;
  while ($w < $n) {
    my $x = syswrite($R, $buf, $n - $w, $w);
    last if !defined $x || $x == 0;
    $w += $x;
  }
  $off += $w;
  print $T time(), ' ', $off, "\n" if $T; # sidecar は raw の後（best-effort）
}

close($R);
close($T) if $T;
