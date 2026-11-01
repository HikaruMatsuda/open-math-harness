# Mac から使うローカル対話構成

Lima VM `open-math` の中で、隔離された対話・プロジェクト・履歴の各サービスを動かし、
Mac の `tools/math_client.py` から Unix ソケット経由で操作する構成です。
モデルは Mac のゲートウェイ `127.0.0.1:10100` を SSH の逆トンネルで VM の
ループバックにだけ公開して使います。

## 起動と状態確認

```sh
cd open-math-harness

# VM 内の構成を起動（トンネル・配置・systemd --user ユニット）
python3 tools/local_chat_start.py

# 状態: ユニットとゲートウェイ
python3 tools/local_chat_start.py --status

# 停止
python3 tools/local_chat_start.py --stop
```

`--status` が `"running": true`、`"unit_active": "active"`、
`"gateway_reachable": true` を返せば利用できます。状態は
`/home/hikaru.guest/open-math-run/chat` に残り、再起動後も履歴とプロジェクトは保持されます。

## 対話

```sh
SOCK=/home/hikaru.guest/open-math-run/chat/ui/ui.sock
CLIENT="python3 tools/math_client.py --socket $SOCK"

# 新しいセッションで話す（未起動なら自動で起動します）
$CLIENT --project demo --session chat1 say "2+2 を計算して"

# 応答を待つ
$CLIENT --project demo --session chat1 wait

# 履歴（user / assistant / tool / model_call など）
$CLIENT --project demo --session chat1 history
```

`say` はセッションが停止していれば先に起動します。起動済みのセッションには触れないので、
進行中のターンを再起動することはありません。明示的に起動・停止する場合は
`start` / `stop` を使います。

## プロジェクトとファイル

```sh
$CLIENT projects                     # プロジェクト一覧
$CLIENT create-project notes         # 新規プロジェクト
$CLIENT --project notes diffs        # リポジトリ一覧（repo）
$CLIENT --project notes diff repo    # 未コミットの変更
$CLIENT files notes                  # repo のファイル一覧
$CLIENT files notes src              # サブディレクトリ
$CLIENT --project notes mkdir src    # ディレクトリ作成
```

エージェントの作業ディレクトリは各プロジェクトの `repo/` です。

## 削除の承認

削除は承認ガードを通ります（`fs remove` は常に `ask`）。

```sh
$CLIENT approvals                    # 保留中の要求（id と digest）
$CLIENT decide 13 <digest>           # 承認
$CLIENT decide 13 <digest> --deny    # 拒否
```

`digest` は一覧に出た値をそのまま渡します。要求が変わっていれば拒否されます。
`shell` から直接実行した `rm` はこのガードを通りません（配備構成と同じ限界）。

## エージェントが使えるもの

`fs`（repo 内の read/list/create/append）、`shell`（repo を作業ディレクトリとする
bubblewrap 内）、`skills`、`memory`、`web`、`jobs`（バックグラウンド実行）、`diffs`、
`lean`（凍結問題と常駐 REPL）、`approvals`、`pdf`。

## 注意

- モデル ID は `opencode-go/deepseek-v4.1-flash` 固定です。前置きを外すと別の quota に回ります。
- `local_chat_start.py --memory-quiet-ms` は記憶の抽出までの待ち時間（既定 300000 ms）です。
- Lean の REPL とツールチェーンは共有 home（`~/open-math-run/chat/home`）にあり、
  見つからない場合は lean サービスが構成から外れます。
