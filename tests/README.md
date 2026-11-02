# テスト

```bash
bash tests/run_all.sh
```

tmux も実ログも要りません（python3 と perl があれば動きます）。セッションはすべてシミュレーションで、
実際のログは入れないでください（`.gitignore` でも除外しています）。

| ファイル | 確かめること |
|---|---|
| `cases.py` | コマンドの区切り全般（2 行プロンプト・PS2・通知など） |
| `cases_bg.py` | 入力受付中の背景ジョブの出力 |
| `cases_lim.py` | 右プロンプト・改行の無い出力・出力の空行・複数行の入力など |
| `test_chunks.py` | `.tsindex` の読み取りの塊で、打った文字と割り込んだ出力を分ける |
| `test_raw_export.py` | `emaki --raw`（tmux なしで動くこと・印の確認・読むファイルの範囲など） |
| `test_logger.sh` | `rl_tee.pl` の sidecar と、`@rl_timestamp` による `.log` の日時 |

`fixtures/*.json` はシナリオのバイト列です。`lim.json` と `chunks.json` は `gen_lim.py`・`gen_chunks.py` で作り直せます。
