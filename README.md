# tmux-robust-logging

[tmux-plugins/tmux-logging](https://github.com/tmux-plugins/tmux-logging) を下敷きに、
**「どの端末に接続していてもログを取りこぼさない」** ことを最優先に作り直したプラグインです。
Linux / macOS / WSL 上の tmux から、Linux・Windows(ssh + PowerShell/cmd)・
ネットワーク機器・シリアルコンソールなど、あらゆる接続先の出力を確実に記録します。

## 設計方針: raw ファースト

元プラグインの主な弱点は「pipe の途中に整形フィルタ（ansifilter 等）を挟む」ことと
「ロギング状態を自前フラグで追跡する」ことでした。前者はフィルタのバッファリングや
クラッシュでログが欠け、後者は実際の pipe 状態とフラグがズレて「取れているつもりで
取れていない」事故を起こします。本プラグインは:

1. **記録は `pipe-pane` + `cat` のみ**。バイト完全一致の `*.raw.log` に直接追記する。
   途中に壊れうるものを挟まないので、取りこぼしが構造的に起きない。
2. **整形は停止時（finalize）に実施**。raw からエスケープシーケンス除去・CRLF 正規化・
   CR/BS 上書き再現をした読みやすい `*.log` を生成する。tmux やフィルタが
   途中で死んでも raw は必ずディスクに残っている。
3. **状態は tmux 本体の `#{pane_pipe}` が唯一の真実**。自前フラグとの desync が起きない。
4. **メタ情報は pane option と状態ファイルに二重化**。ペインが消えた後でも
   フック（`pane-exited` / `pane-died`）や `--sweep` で finalize できる。

## 必要環境

- tmux **3.1 以上**（pane option は 3.1 で導入されたため。インデックス付きフックは 3.0 から）
- `bash`, `cat`, `sed`, `awk`（ほぼ必ずある）
- あれば使うもの: `perl`（高品質サニタイズ）, `split`（サイズローテーション）, `iconv`（文字コード変換）,
  `python3` 3.7+（HTML 出力コマンド `emaki`。標準ライブラリのみで追加インストール不要）,
  Fira Code フォント（emaki の HTML の表示用。`sudo apt install fonts-firacode`。無ければ代替フォント）

## インストール

TPM の場合:

```
set -g @plugin 'yourname/tmux-robust-logging'
```

手動の場合は `.tmux.conf` に:

```
run-shell /path/to/tmux-robust-logging/robust-logging.tmux
```

## キーバインド（元プラグイン互換）

| キー | 動作 |
|---|---|
| `prefix + P` | ロギングのトグル（開始 / 停止+整形） |
| `prefix + M-p` | 表示中画面をテキスト保存 |
| `prefix + M-P` | スクロールバック全履歴を保存 |
| `prefix + M-c` | スクロールバック消去 |
| `prefix + M-e` | このセッションのログを HTML に書き出す（[emaki](#emaki-セッションを-1-枚の絵巻に)。結果はステータス行に表示） |

## emaki: セッションを 1 枚の「絵巻」に

tmux の中で `emaki` と打つと、**そのセッションのそれまでの内容**（閉じたペインも含む）を、
コマンド単位に折りたためる **1 枚の HTML** に時系列順で書き出します。
絵巻（えまき）＝出来事を時間の流れに沿って一続きの巻物に描いたもの。巻物（scroll）は
端末のスクロールバックとも掛けています。

- 左に時刻の列と縦線が並ぶダークなタイムライン。上にホスト・書き出しの時刻・期間・コマンド数。
  コマンドと出力のフォントは Fira Code（入っていなければ代替フォント。`--embed-font` で HTML に埋め込み）
- 各コマンドがトグル（プロンプトの `❯` が開閉の印）。開くと見出しのコマンドが折り返して全文になり、
  中にプロンプト・開始時刻・所要時間・終了コード・cwd と出力（行番号付き）。コマンドは中で繰り返さない
- **色は端末で見えていたまま**: 出力も、見出しのコマンド（シンタックスハイライト）も、プロンプト（p10k）も、
  画面に出ていた色で表示（補完の一覧やジョブの通知の後に描き直された入力は、最後に描き直されたときの色）。
  配色は書き出すときに **QTerminal の設定**（配色の名前）と、QTerminal が読むのと同じ場所の `.colorscheme`
  （`/usr/share/qtermwidget6/color-schemes` → `~/.local/share/qterminal/color-schemes` →
  `~/.config/qterminal.org/color-schemes`）から読む。読めなければ Kali 標準の Kali-Dark（そのときは HTML の
  先頭に注記）。太字は QTerminal と同じく明るい色。使った配色の名前はページの一番下に表示。
  明るい配色（Kali-Light など）では、開いたカードの中は暗い文字になり、見出しのコマンドは端末の背景色の上に描く
- 検索（ハイライト、一致へジャンプ、大文字小文字・正規表現・コマンド/出力の範囲切替）
- フィルタ（失敗したコマンドを隠す / リモート操作だけ / ペインごと）。`?` でキーボード操作の一覧
- **evil-winrm / ssh / nc / msfconsole / python / mysql など**の中で打ったコマンドは、
  リモート側のプロンプトで分けて入れ子に表示（推定。終了コードは親の失敗には数えない）
- p10k の右プロンプトの `✘ 2` は、次の行ではなく**実際に失敗したコマンド**に付け替え
- 全画面アプリ（vim / less / top）の画面はプレースホルダに畳む
- 外部依存なしの単一 HTML（オフラインで開ける・共有しやすい）。ファイルは 600

### 使い方

```bash
emaki                 # 今のセッション → ~/tmux-logs/emaki/emaki_<セッション>_<日時>.html
emaki --open          # 書き出してブラウザで開く
emaki -s work         # 別のセッションを指定
emaki --all           # tmux サーバ上の全セッションを 1 枚に（ペイン名の後ろにセッション名: 0:zsh.0 · work）
emaki --raw ~/tmux-logs/tmux_work_*.raw.log   # 終了したセッションのログから（tmux は要らない）
emaki -o report.html  # 出力先を指定
emaki --embed-font    # Fira Code を HTML に埋め込む（別の PC で開いても同じ見た目。数百 KB 増える）
emaki --palette Kali-Light   # 配色を指定（QTerminal の配色の名前 / .colorscheme のパス / emaki = HTML の既定）
emaki --help
```

### 終了したセッションのログから書き出す

`emaki --raw <ログ>...` は、指定したログファイルから同じ HTML を書き出します。セッションが終わった後でも、tmux サーバが
無くても、別の PC に持ってきたログでも動きます。複数指定すると時系列で 1 枚にまとまります（1 つのセッションの
全ペインなら、ファイル名の `tmux_<セッション>_` でまとめて指定できます）。

- 既定の出力先は、最初のログと同じ場所の `emaki/`（`-o` で指定できます。`@rl_emaki_dir` があればそちら）
- 同じフォルダの sidecar（`*.tsindex`）と索引（`.rlindex/`）も読みます。無くても書き出せます
- シェル統合の印は、nonce の台帳（`~/.local/state/emaki/nonces`）で確かめられたものだけを使います。別の PC のログなど、
  確かめられないログは入力行から推定します（コマンドは分かれますが、終了コードは出ません）
- **raw は既定では整形後に消えます**。後から書き出したいなら `set -g @rl_keep_raw on` にしてください。
  raw が無いログは `*.log` も指定できますが、プロンプトの形からの推定になります（色・終了コードなし）

`emaki_<セッション>_latest.html` は常に最新版へのリンクです。tmux からは `prefix + M-e` でも書き出せます
（ペインの表示は変えず、結果や失敗はステータス行に出ます。詳しい原因はシェルで `EMAKI_DEBUG=1 emaki`）。
1 つのログが壊れていても、そのペインだけ注記を付けて残りは書き出します。注記（含めなかったログ・
ロギングを止めた後の出力が無いことなど）は端末にも `注記: …` と表示されます。

### セットアップ（任意だが推奨: シェル統合）

`~/.zshrc`（bash なら `~/.bashrc` に `.bash` 版）に 1 行:

```bash
source ~/.tmux/plugins/tmux-robust-logging/shell/emaki-integration.zsh
```

これで `emaki` コマンドが使えるようになり、さらにシェルが**コマンド境界の印**
（OSC 133 / 633 / 7。画面には何も出ない）を出すので、コマンドの区切り・**終了コード**・
実行ディレクトリが**推定ではなく正確**になります。入れなくても動きます（その場合はプロンプトの
形や zsh/bash の入力受付の印から推定）。シェル統合を使わない場合は
`alias emaki='python3 ~/.tmux/plugins/tmux-robust-logging/bin/emaki'` でも可。

印には**シェルごとの合言葉**（`;emaki=<乱数>:<コマンド番号>`）が付き、合言葉は手元の
`~/.local/state/emaki/nonces`（`$XDG_STATE_HOME` があればその下）にだけ記録されます。
コマンドの出力（攻撃対象の Web ページやファイルの中身、`cat` したログなど）に偽の印が混ざっても、
合言葉や番号が合わないので emaki は無視します。存在しないコマンドや終了コードを出力から
捏造されることはありません（ssh 先や入れ子のシェルの印も「出力」として扱い、プロンプトの形で入れ子に分けます）。
入れ子のシェル・`exec zsh` は親シェルの合言葉も記録するので、入れ子の中でロギングを始めた場合や
時計が戻った後の `exec` でも区切りが崩れません。

bash 版の注意: bash は実行するコマンドの文字列を渡す仕組みが無いため、コマンド文字列は画面（入力行）から
読みます（詳細欄に「コマンド文字列は画面から読んだもの」と表示）。プロンプトで待っている間に背景ジョブの
出力が入力行に割り込むと、その文字列がコマンドとして記録されることがあります。zsh 版にはこの制限はありません。

### 精度の目安

| 状況 | コマンドの区切り | 終了コード |
|---|---|---|
| シェル統合あり | 正確 | 正確 |
| シェル統合なし（zsh / bash） | 入力行から推定（高精度） | p10k の `✘ N` 表示から推定（右プロンプトが消えるほど長い入力の直前は「不明」） |
| raw が残っていない（`@rl_keep_raw off` で整形済み） | プロンプトの形から推定 | なし（色もなし） |
| ログの無いペイン | スクロールバックから推定（時刻なし） | なし |

改行で終わらない出力（`curl -s` / `echo -n` / 末尾改行の無いファイルの `cat` など）、入力受付中に
背景ジョブが出した出力、`for>` などの継続行、接続が切れて閉じられなかった全画面アプリ（vim / less）も
取りこぼしません。

各コマンドの時刻は sidecar（`*.tsindex`）から読みます。sidecar は perl があれば常に記録されます
（`@rl_timestamp` は整形後の `.log` の行頭に日時を付けるかどうかだけを決めます）。sidecar にはロガーが 1 回に
読み取った塊の境目も残っているので、シェル統合が無くても、入力行に割り込んだ背景ジョブの出力と打った文字を分けられます
（人が打った文字は 1 打ごとの塊、出力は別の塊で届くため）。

raw を残すには `@rl_keep_raw on`（`@rl_raw_dir` 指定時は既定）を推奨。既定では raw と sidecar は整形後に消えます。

### 「rtlog」について（調査結果）

端末セッションを記録・HTML 化するツールとしての「rtlog」は存在しませんでした（apt / PyPI / npm /
crates.io いずれも該当なし）。同名のものは OpenRTM のロボット用データ記録（rtshell）、Fastly の
Go SDK のリアルタイムログ送信、Twitter を HTML アーカイブ化する古い Ruby gem で、いずれも無関係です。
外部ツールは不要で、本プラグインが既に持つデータを土台にするのが最適です:

| | 本プラグインのログを使う（emaki） | tmux の capture-pane だけで作る |
|---|---|---|
| 量 | 全部（raw に全バイト） | `history-limit`（既定 2000 行）まで |
| 閉じたペイン | 残る | 消える |
| 時刻 | 各コマンドの時刻（`@rl_timestamp`） | なし |
| コマンドの区切り | シェル統合の印・入力の印が raw に残る | 印は消えている（推定のみ） |
| tmux 再起動・クラッシュ | 生き残る | 消える |

ログが 1 つも無いペインだけは capture-pane で補います。

## オプション

```
set -g @rl_log_dir "~/tmux-logs"      # 整形済みログの保存先（書けない時は TMPDIR へフォールバック）
set -g @rl_raw_dir "~/tmux-logs/raw"  # raw だけ別ディレクトリへ（既定: 整形ログと同じ場所）
                                      # ※これを指定すると @rl_keep_raw 未設定でも raw を残す
set -g @rl_auto_start on              # 全ペインを自動でロギング（新規ペインも）
set -g @rl_capture_backlog on         # 開始時に既存の画面+スクロールバックをログ先頭へ取り込む
set -g @rl_keep_raw on                # 整形後も raw を残す（既定 off）
set -g @rl_max_size_mb 100            # raw をこのサイズで分割ローテーション（既定 0=無制限）
set -g @rl_convert_from CP932         # 整形時に iconv で UTF-8 へ変換（Windows CP932 等）
set -g @rl_timestamp on               # 各行の先頭に日時を付ける（WinRM 等で有効・後述）
set -g @rl_timestamp_format "[%Y-%m-%d %H:%M:%S] "  # strftime 形式（既定はこれ）
set -g @rl_history_escapes on         # 履歴保存に色エスケープを含める（既定 off）
set -g @rl_exit_sweep off             # サーバ終了検知ウォッチドッグを止める（既定 on）
set -g @rl_live_clean 5               # ロギング中も逐次整形: 出力が静止するたびに .log を
                                      # 再生成（最短この秒数間隔）。既定 0 = finalize 時のみ
set -g @rl_growth_warn_mb 10          # raw がこの MB/2秒 を超えて成長し続けたら警告（0=off）
set -g @rl_quiet on                   # ステータスメッセージを出さない
set -g @rl_key_toggle P               # キー変更
set -g @rl_key_screen M-p
set -g @rl_key_history M-P
set -g @rl_key_clear M-c
set -g @rl_key_emaki M-e              # emaki の書き出しキー（none で割り当てない）
set -g @rl_emaki_dir "~/tmux-logs/emaki"  # emaki の出力先（既定: <ログ保存先>/emaki）
set -g @rl_emaki_open on              # キーで書き出したらブラウザで開く（既定 off）
set -g @rl_emaki_embed_font on        # emaki の HTML に Fira Code を埋め込む（既定 off）
set -g @rl_emaki_palette "auto"       # 出力の色に使う配色（既定 auto = QTerminal の設定から。名前・パス・emaki も可）
```

emaki の環境変数: `EMAKI_PALETTE`（`--palette` と同じ。tmux の設定より後に見る）、`EMAKI_DEBUG=1`（失敗時にトレースバックを表示）、`EMAKI_NONCE_FILE`（シェル統合の
合言葉の記録ファイルを変える。tmux サーバの環境にも同じ値が必要）、`EMAKI_TMUX`（tmux の呼び方。
別ソケットなら `EMAKI_TMUX="tmux -L work"`）。

### tmux 起動の瞬間からログを取る

オプションを**プラグインのロードより前に**書くこと（TPM なら `run '~/.tmux/plugins/tpm/tpm'` より前）:

```
set -g @rl_auto_start on
set -g @rl_capture_backlog on
run-shell /path/to/tmux-robust-logging/robust-logging.tmux
```

サーバ起動時はロード時の全ペイン走査と `session-created` フックが最初のセッションを
カバーし、以後の新規ウィンドウ・ペインは `after-new-window` / `after-split-window`
フックが自動開始する。ペイン誕生から pipe 確立までの数ミリ秒に出た出力は pipe には
乗らないため、`@rl_capture_backlog on` が開始時点の画面+スクロールバックをログ先頭に
取り込んで隙間を埋める（後からロギングを有効化した場合は過去のスクロールバックごと
保存される。取り込まれるのは capture-pane のレンダリング結果で raw バイトではない）。

ファイル名は `tmux_<session>_<win番号>-<win名>_p<pane番号>_<日時>.log` 形式。
セッション名・ウィンドウ名は Unix/Windows 両方で不正になる文字（`/ \ : * ? " < > |` 等）を
`_` に置換してから使うため、共有フォルダに置いても壊れません。

## 対応しているエッジケース

**接続先が Windows のとき（ssh + PowerShell / cmd）**
- CRLF 行末 → 整形時に LF へ正規化
- PSReadLine の激しいカーソル移動・再描画エスケープ → CSI/OSC 除去に加え、
  行内の CR / BS / 編集 CSI（カーソル移動・消去・削除・挿入、および zle が入力の
  折返し時に使う縦移動 CUU/CUD）を「小さな端末エミュレーション」（複数行セルバッファ、
  UTF-8 全角 2 桁幅対応）で再生し、上書き後の実表示を再構成。プログレスバーは最終状態が残り、
  **backspace で日本語を消しても文字化けせず**、入力行が画面右端で折り返しても
  プロンプト行が消えない
- CP932 (Shift_JIS) 出力 → raw はバイトそのまま保存。`@rl_convert_from CP932` で
  整形時に UTF-8 化（raw は無傷なので変換に失敗してもやり直せる）
- UTF-16 が混ざり NUL バイトだらけになるケース → sanitize が NUL を除去
  （`@rl_convert_from UTF-16LE` の方が正確）

**tmux / プロセスの異常系**
- コマンド自身の終了 → `pane-exited` / `pane-died` フックが finalize。
  ペイン消滅で pane option が読めなくても状態ファイルから raw の場所を復元
- `kill-pane` / `kill-window` / `kill-session` → tmux はこれらで pane-exited を
  **発火しない**（tmux ソース確認済み）ため、`window-layout-changed` /
  `window-unlinked` / `session-closed` フックの `--sweep` が回収する
- 最後の `exit`・kill-server・サーバクラッシュ → プラグインが常駐させる
  **ウォッチドッグ**（サーバから setsid でデタッチした監視プロセス、既定 on）が
  サーバの死を検知し、数秒以内に自動で `--sweep` を実行して回収・整形する。
  ウォッチドッグ自体が死んでいた場合も、次回ロード時の `--sweep` が**過去サーバの
  PID ディレクトリも走査**して回収する（生きている別サーバのものはスキップ）。
  raw 自体は常にディスクにあるので**データは失われない**
- stop とフックが同時に走る → メタファイル横の mkdir ロックで二重 finalize を防止
- finalize 中にユーザーが再度ロギングを開始 → finalize は「自分が読んだ base」
  以外の pipe・メタ情報に触らないので、新セッションを壊さない
- `@rl_keep_raw` / `@rl_convert_from` は**ロギング開始時の値をメタに記録**して
  finalize で使う。サーバ死亡後の watchdog / sweep は tmux オプションを読めないため、
  記録しないと「keep_raw on なのに exit 時に raw が消える」事故になる
- sanitize の失敗・整形結果が空・iconv の異常 → raw を消さずに残す
  （`sanitize.sh` は pipefail + フィルタ終了コードの直接検査。iconv は
  「不正バイトを捨てただけで exit 1 を返す」仕様のため出力の有無で成否判定）
- pipe 停止直後の書き残し → finalize がファイルサイズが安定するまで待ってから処理

**状態管理・競合**
- 別ツールが既に `pipe-pane` している → 検知して**触らない**（元プラグインは奪って壊す）
- 開始後に本当に pipe が張れたか `#{pane_pipe}` で検証、失敗ならその場で通知
- 同一秒に同一ペインで停止→開始 → raw / 整形済み .log / part ファイル全てに対して
  ファイル名衝突を検出して PID を付加

**ファイル・パスまわり**
- プラグインのパスに空白・括弧・`'`・`$`・`"` があっても動く。set-hook に保存する
  コマンドは発火時に tmux の二重引用符レイヤを再度通るため、sh クォートに加えて
  そのレイヤ用のエスケープも重ねている
- パスやファイル名の `#` は tmux の format 展開前にエスケープ（`##`）
- セッション名が `../../etc` や `C:` のような名前でも安全なファイル名に落とす
- ログは `umask 077`、保存ディレクトリは 700 で作成（ログには秘密が写り込むため）
- 保存先が書き込み不能 → TMPDIR へフォールバックし、開始時に書き込み検証

**容量**
- `@rl_max_size_mb` 指定時は `split` がローテーションしながら書くため、
  暴走した出力でもディスクを無限には食わない（finalize で結合される）

## 既知の制限（意図的なトレードオフ）

- 整形後ログはあくまで「端末で最終的に見えた行」の近似。カーソル移動で画面の
  任意位置に書く TUI（vim, htop 等）のログは raw でしか意味を持たない
  （これはどの行指向ロガーでも同じ。必要なら `@rl_keep_raw on` で raw を残し、
  `less -R` や端末リプレイで見る）
- 整形後の `.log` に日時は既定では付かない。必要なら `@rl_timestamp on`。
  raw 本体はバイト完全一致のまま、別 sidecar で時刻を持つ方式（「ログに日時を入れる」節）
- perl 不在時の awk フォールバックは行単位処理で、行をまたぐ OSC/DCS の除去が不完全。
  また BS はエミュレートせず除去のみ（上書き再現は perl 経路のみ）
- 非 UTF-8 エンコーディング（CP932 等）の BS 編集はバイト単位の近似のまま
  （文字境界が判定できないため。raw は無傷なので必要なら後から変換し直せる）
- 行レンダラの意図的な近似: TAB はタブストップを再現せず 1 セル扱い、
  結合文字・ZWJ 等のゼロ幅文字は 1 桁扱い（CR/BS と同一行で混ざった場合のみ影響）
- 画面の折返し幅は raw からは分からないため、入力行が折り返した際の
  autosuggestion 消去は完全には再現できず、提案テキストがコマンド行の末尾に
  残って見えることがある（コマンド自体は正しく記録される）
- `capture-pane` 系（画面・履歴保存）は `history-limit` を超えた分は取れない。
  確実な記録は pipe-pane ロギングを使うこと
- ログにはパスワードのエコーバックや秘密情報が写り込みうる。保存先の扱いに注意

## ⚠ ロギング中のペインで raw を cat しない

ロギング中のペイン内で**自分の raw を `cat` / `tail -f` すると自己増殖ループになる**
（cat の出力 → pipe-pane が同じ raw に追記 → cat がそれを読む → …）。
ファイルがパイプバッファ (~64KB) より大きいと EOF に追いつけず、Ctrl-C するか
ディスクが埋まるまで増え続ける。これは pipe-pane ロギング全般の性質。

安全な見方: `less -R`（画面分しか出力しない）/ ロギングしていない別ペイン・
tmux 外から見る / `@rl_live_clean` の整形済み `.log` を見る（別ファイルなのでループしない）。
ウォッチドッグが暴走成長（既定: 10MB/2秒 × 3 サイクル連続）を検知すると警告を表示する。

## ログに日時を入れる（WinRM・リモート接続でも）

これまでのログの日時は、ローカルの zsh プロンプト(PS1)が描画した文字列を拾っていた。
そのため **WinRM / SSH でリモートに入るとリモート側プロンプトに日時が無く、日時が
記録されない**。`@rl_timestamp on` にすると、**ロガー自身が時刻を刻む**ので、接続先が
何であれ（WinRM・SSH・シリアル・ネットワーク機器）各行に日時が入る。

```
set -g @rl_timestamp on
set -g @rl_timestamp_format "[%Y-%m-%d %H:%M:%S] "   # strftime。省略時はこれ
```

**raw の完全性は保ったまま実現している**のがポイント:

- 記録は小さな perl ヘルパー([rl_tee.pl](scripts/rl_tee.pl))が担う（perl が無ければ `cat`）。
  raw には**バイト完全一致**でそのまま追記し、別ファイル `*.tsindex`（sidecar）に
  「いつ・raw の何バイト目まで書いたか」だけを best-effort で記録する。
  ヘルパーは各チャンクで**先に raw を書き切ってから** sidecar に書くので、途中で死んでも
  raw は常に完結（raw ファースト原則は不変）。
- 日時は raw には入れず、**整形時**に sidecar のオフセット↔時刻対応から各行に付ける。
  raw を後から別形式で整形し直すこともできる。

制限:
- perl が必要（無い環境では日時なしで記録。ヘルパーが perl なので当然）
- サイズ分割モード(`@rl_max_size_mb`)とは併用不可（オフセットが part をまたぐため、
  分割時は自動で日時オフフォールバック）
- 空行（プロンプト間のスペーサ等）には付けない。折り返した行は先頭行のみ
- 各行は「その行の先頭バイトが書かれた時刻」で刻む（行が \n で完成した時刻ではない）。
  これによりプロンプト行は入力に数秒かけてもプロンプト描画時刻(=PS1)で記録され、
  コマンド出力も出現時刻で記録される。精度はチャンク単位だが対話利用では実質リアルタイム

## ロギング中に整形済みログを読む

`@rl_live_clean <秒>` を設定すると、ウォッチドッグが「出力が約 2 秒静止した」
raw を検知するたびに整形済み `.log` を atomic に再生成する（コマンド終了 = 出力静止
なので、実質コマンド実行のたびに `.log` が更新される）。シェルフック不要のため
ssh 先やネットワーク機器など**どんな接続先でも機能する**。
raw → 整形は純粋な変換なので何度再生成しても安全で、最終版は従来通り finalize が作る。
注意: サイズ分割モード（`@rl_max_size_mb`）のペインは finalize 時のみ整形。

## 手動での後始末

pipe が残ったまま何かおかしくなった場合:

```bash
tmux pipe-pane -t %5                      # pipe を閉じる
/path/to/scripts/finalize.sh '%5'         # 整形して片付け
/path/to/scripts/finalize.sh --sweep      # 残骸を全部回収
```

raw だけ手に入れて後から整形したい場合:

```bash
/path/to/scripts/sanitize.sh < foo.raw.log > foo.log
RL_ICONV_FROM=CP932 /path/to/scripts/sanitize.sh < foo.raw.log > foo.log
```
