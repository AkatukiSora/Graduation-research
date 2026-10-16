# 規制ポイントの仮データ投入（seed）

`backend/app` に埋め込んだ合成データを、PostgreSQL（PostGIS）の `regulation_datasets` / `regulation_points` / `regulation_candidates` へ投入する仕組みです。投入は `migrate`（スキーマ適用の実行ファイル）が、環境変数で指定されたときだけ、スキーマ適用の後に行います。API の起動時には実行されません。ローカルでの確認用に、同じ処理を単独で実行する `cmd/seed` も用意しています。

## 仮データの性質

- 埋め込み対象は **合成データ**です。実在の標識・座標・境界・規制情報は含みません。
- すべてのポイントは `direction_status = unknown`、`validity_status = unknown`、`direction_degrees = NULL` です。
- データセットは `synthetic = true`、`production_approved = false` で保存されます。投入によって承認状態が変わることはありません。
- 候補（`regulation_candidates`）の `candidate_bearing` は、隣り合うポイントどうしの幾何的な方位です。標識の向きではなく、公式の方向としては扱えません（`bearing_semantics = candidate_point_neighbor_only`）。

## 埋め込みデータ

```text
internal/seeddata/data/<dataset-id>/
├── manifest.json                 # format/counts/digest/provenance
├── dataset.json                  # v1 points + candidates only
└── *.jsonl.gz                    # v2 bounded points/candidate shards
```

`synthetic-v1` は12ポイント・58候補、`synthetic-v2` は同じ合成 fixture を schema v2 の gzip JSONL shard に変換したものです。v1 のファイルとダイジェストは変更せず、v2 はロード時に全 shard を逐次検証し、ポイント／候補の全 object 配列を保持しません。いずれも合成専用で、実データ・取得証拠・推定精度の証明ではありません。データはビルド時にバイナリへ埋め込まれ、実行時はファイルシステムから読み込みません。

## 検証（データベース不要）

```sh
cd backend/app
go run ./cmd/seed --validate-only --dataset synthetic-v1
go run ./cmd/seed --validate-only --dataset synthetic-v2
```

`--dataset` を省略すると、埋め込まれたすべてのデータセットを検証します。次の項目を確認し、違反があれば理由コード付きのメッセージを表示して終了コード 1 で終了します。

- v1 は `manifest.json` / `dataset.json`、v2 は manifest counts / 全 shard の SHA-256 と件数
- ID と `raw_uid`（32 文字以内）の一意性、経度 [-180, 180]・緯度 [-90, 90] の有限値
- 全ポイントが方向・有効性とも unknown で、データセットが合成かつ未承認であること
- 候補が同じデータセット内の別ポイントを参照し、順位が 1 から連続し、距離が 0 以上、方位が [0, 360) であること
- v2 は manifest / JSONL key と gzip / UTF-8 / LF / SHA-256 を厳密に検証し、manifest 1 MiB・行64 KiB・shard展開4 MiB・bundle展開／圧縮各64 MiB・index 128 MiBの上限を適用すること
- v2 の source-geometry / nearest evidence は埋め込みポイント・候補と照合し、未登録の OSM 推定 method は拒否すること
- source geometry evidence の入口座標から方位を再計算し、manifest に記録された source CSV SHA と親ポイント座標との整合を確認しますが、これは埋め込み bundle 内の自己整合性検証です。埋め込みデータだけでは、その入口座標が原本 CSV に実在したこと（出自）は証明できません。出自は、後続の実データ生成工程が原本 raw から fresh に再生成し、独立レビューで照合して確認する責務です。これらの検証は、推定方向の現地での正しさ、通知の安全性、現地確認済みであること、第三者権利の承認を意味しません。

成功時は件数とダイジェストを JSON で 1 行出力します。

## 投入

### マイグレーションと一緒に投入する（通常の経路）

`migrate` は、Atlas でスキーマを適用した後、環境変数 `SEED_DATASET_IDS` に列挙したデータセットを投入します。

```sh
SEED_DATASET_IDS=synthetic-v1 /migrate
```

- 未設定または空なら、スキーマの適用だけを行い、何も投入しません。
- 複数指定はカンマ区切りです（例：`a-1,b-2`）。空白と重複は無視し、不正な ID は拒否します。
- すべてのデータセットを先に検証し、問題があれば何も書き込まずに失敗します。
- 冪等です。同じ内容が保存済みなら `unchanged` を返すため、毎回のデプロイに含めても安全です。
- 接続は `migrate` のものを使うため、投入専用の経路や権限は増えません。

出力は 1 データセットにつき 1 行の JSON です。

```json
{"dataset_id":"synthetic-v1","status":"inserted","points":12,"candidates":58}
```

### 単独で投入する（ローカル確認用）

```sh
cd backend/app
go run ./cmd/seed --apply --dataset synthetic-v1
```

`--apply` は `--dataset` を明示したときだけ実行されます。接続先は API・`migrate` と同じ環境変数（`DB_HOST`、`DB_PORT`、`DB_NAME`、`DB_USER`、`DB_PASSWORD` または `DB_IAM_AUTH`、`DB_SSL_MODE`、`AWS_REGION`）で指定します。資格情報はログに出力しません。コンテナイメージには含めません。

事前に `migrate` で `regulation_*` テーブルを作成してください。ランタイム用の読み取り専用ロールには INSERT 権限がないため、投入には書き込み権限を持つ接続（マイグレーションと同じ主体など）を使用します。ロールやシークレットは作成しません。

### 投入の動作

投入は 1 つのトランザクションで行います。

1. データセット ID をキーにしたアドバイザリロックを取得し、同じデータセットの同時投入を直列化します。
2. 同じ ID・同じダイジェストが既に存在すれば何も書き込まず `unchanged` を返します。
3. 同じ ID で異なるダイジェストが存在する場合、または同じダイジェストが別の ID に存在する場合は書き込まずに拒否します。
4. データセット、ポイント、候補を追加し、保存件数とマニフェストの件数、unknown 状態のポイント数を照合してからコミットします。途中でエラーが起きた場合はすべてロールバックします。

他のデータセットと既存の `stop_signs` テーブルは変更しません。出力例は次のとおりです。

```json
{"dataset_id":"synthetic-v1","mode":"apply","status":"inserted","points":12,"candidates":58,"content_sha256":"…"}
```

## 埋め込みデータの再生成と確認

`db/seed/synthetic/build_dataset.py` は固定の合成 CSV を generation I/O CLI で処理し、v1 fixture を作ります。`db/seed/synthetic/build_dataset_v2.py` はチェックイン済みの synthetic-v1 fixture だけを決定的に schema v2 shard へ変換します。どちらも実データを取得せず、v2 生成物には CSV / boundary / input manifest / generator / method metadata の SHA-256 と件数を記録します。

```sh
cd backend/generation_io
uv sync --locked --all-groups
uv run --locked python ../app/db/seed/synthetic/build_dataset.py --check
cd ../app
python3 -B db/seed/synthetic/build_dataset_v2.py --check
```

`--write` は対象 fixture を意図的に更新するときだけ使用します。v1 と v2 の生成物はそれぞれ別の `--check` でバイト一致を確認します。


## API が参照するデータセット

API（`cmd/api`）は起動時にデータベースへ接続して疎通を確認し、設定 `STOP_POINT_DATASET_ID` で指定した 1 つのデータセットの地点だけを返します。未設定のときは `synthetic-v1` を使います。実データを投入した後は、本番の環境で明示的にデータセット ID を設定してください。値が空、または小文字英数字と `.` `_` `-`（先頭は英数字、64 文字以内）以外を含む場合は起動に失敗します。

- 仮データの地点は方位が不明なので、`az` は `null`、`directionStatus` と `validityStatus` は `unknown`、`synthetic` は `true`、`notificationEligible` は `false` で返ります。候補方位（`regulation_candidates`）は API に出力されません。
- `notificationEligible` は保存値ではなく、データセットが本番承認済みで合成でなく、方位が `confirmed` かつ地点が `valid` のときだけ `true` になります。
- データベースに接続できないときは、`/api/v1/health` が 503（`{"status":"degraded","database":"unavailable"}`）、地点の取得が 503（`error.code` は `database_unavailable`）を返します。
- IAM 認証の場合は、新しい接続を開くたびに認証トークンを作り直します。`DB_PASSWORD` を設定した場合は固定のパスワードを使い、AWS には問い合わせません。

## テスト

```sh
cd backend/app
go test ./...
go vet ./...
SCRATCH="$HOME/.cache/graduation-seed-integration"
mkdir -p "$SCRATCH"
python3 -B db/seed/tests/run_integration.py --toolchain-report /path/to/validated-toolchain-report.json --scratch-dir "$SCRATCH" --log "$SCRATCH/integration.log"
python3 -B internal/handler/tests/check_openapi.py --toolchain-report /path/to/validated-toolchain-report.json
git diff --check
```

後者は使い捨てのローカル PostGIS クラスタを作成し、全マイグレーションを適用して、seed／API repository／接続プールの統合テストを実行します。seed では v1・v2 の初回投入と再実行、混在状態の保存・再生、後段 JSONL decode／gzip CRC／context cancellation／候補 INSERT の失敗時に行が残らないこと、並行投入と件数照合を確認します。さらに、`migrate` が v1 と v2 を同じ実行で投入・再実行し、未知 ID は書き込み前に拒否することを検証し、終了時にクラスタを停止・削除します。scratch ディレクトリと read-only の toolchain report は別のパスにしてください。統合テストは `TEST_DATABASE_URL` が未設定ならスキップされ、ホストがループバックまたはローカルのソケット以外の場合は実行を拒否します。

`check_openapi.py` は、ハンドラが実際に返す JSON と `backend/docs/openapi.yaml` のプロパティ・必須項目・エラーコードの一致を確認します（データベースは不要で、PyYAML または ruamel.yaml が必要です）。

## 検証済みの範囲と未検証の範囲

- 検証済み：`go test ./...`、`go vet ./...`、v1 / v2 fixture の再生成チェック、v2 `--validate-only`、ローカル PostGIS 統合テスト15件（v1/v2投入・再実行を含む）、OpenAPI とハンドラ出力の一致。
- v2 の10,000ポイント／60,000候補 fixture は、streaming load と全 object 配列を保持しないことを確認し、Linux のプロセス high-water RSS は45,104 KiB（512 MiB 未満）。
- 未検証：コンテナイメージ（`/api`、`/migrate`）の実ビルド、AWS／RDS（IAM 認証）上での投入と実際のトークン発行、ECS タスクでの実行（`SEED_DATASET_IDS` を渡すインフラ設定は別途必要）、Lambda 上での `cmd/api` の起動、GitHub Actions での実行。
- `run_integration.py` の `--toolchain-report` は、PostgreSQL 17 と PostGIS を展開したディレクトリ（`postgres/root`）と同じ場所に置く、ツールチェーンの記録ファイル（JSON）です。リポジトリだけでは再現できず、同等の使い捨て環境を用意する必要があります。
- `build_dataset.py --check` が全体でバイト一致するのは、`uv.lock` で固定した環境で実行した場合です。マニフェストには実行時のライブラリ版が記録されるため、別の環境では `dataset.json` のダイジェストだけが一致します。
