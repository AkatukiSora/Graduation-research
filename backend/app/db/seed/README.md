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
├── manifest.json   # 版・月・地域・件数・dataset.json の SHA-256・生成元・出典
└── dataset.json    # points と candidates
```

現在は `synthetic-v1`（12 ポイント、58 候補、対象月 `202701`、地域 `synthetic:rectangle`）です。ファイルはビルド時にバイナリへ埋め込まれ、実行時にファイルシステムからは読み込みません。

## 検証（データベース不要）

```sh
cd backend/app
go run ./cmd/seed --validate-only --dataset synthetic-v1
```

`--dataset` を省略すると、埋め込まれたすべてのデータセットを検証します。次の項目を確認し、違反があれば理由コード付きのメッセージを表示して終了コード 1 で終了します。

- マニフェストと `dataset.json` の SHA-256、件数（ポイント・候補・棄却 0・領域外 0）
- ID と `raw_uid`（32 文字以内）の一意性、経度 [-180, 180]・緯度 [-90, 90] の有限値
- 全ポイントが方向・有効性とも unknown で、データセットが合成かつ未承認であること
- 候補が同じデータセット内の別ポイントを参照し、順位が 1 から連続し、距離が 0 以上、方位が [0, 360) であること

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

`db/seed/synthetic/build_dataset.py` は、固定の合成 CSV（`db/seed/synthetic/input/`）を `backend/generation_io` の CLI（`prepare-input`、`generate`、`verify`）で処理し、`internal/seeddata/data/synthetic-v1/` の JSON を作ります。乱数は使用しません。

```sh
cd backend/generation_io
uv sync --locked --all-groups
uv run --locked python ../app/db/seed/synthetic/build_dataset.py --check   # 再生成してバイト単位で一致を確認
uv run --locked python ../app/db/seed/synthetic/build_dataset.py --write   # チェックイン済みファイルを更新
```

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
python3 db/seed/tests/run_integration.py --toolchain-report <toolchain-report.json>
python3 internal/handler/tests/check_openapi.py --toolchain-report <toolchain-report.json>
```

後者は使い捨てのローカル PostGIS クラスタを作成し、全マイグレーションを適用して `internal/seed` の統合テスト（初回投入、再投入の no-op、ダイジェスト不一致の拒否、不正データ、ロールバック、並行投入、件数照合）と `cmd/seed` の実行ファイルでの投入を検証します。さらに、実際の `migrate` 実行ファイルを空のデータベースに対して実行し（環境変数なしならスキーマ適用のみ、指定ありなら投入、再実行は `unchanged`、存在しない ID は何も書かずに失敗）、続けて、投入済みのデータに対する API のリポジトリ（半径検索と並び順、境界、方位不明の表示、ID 検索、他データセットの分離、DB 障害の扱い）と、接続ごとのパスワード解決を検証し、終了時にクラスタを停止・削除します。統合テストは `TEST_DATABASE_URL` が未設定ならスキップされ、ホストがループバックまたはローカルのソケット以外の場合は実行を拒否します。

`check_openapi.py` は、ハンドラが実際に返す JSON と `backend/docs/openapi.yaml` のプロパティ・必須項目・エラーコードの一致を確認します（データベースは不要で、PyYAML または ruamel.yaml が必要です）。

## 検証済みの範囲と未検証の範囲

- 検証済み：Go の単体テスト、`--validate-only`、使い捨てのローカル PostgreSQL／PostGIS に対する統合テスト（投入・再投入、API のリポジトリ、接続ごとのパスワード解決）と `cmd/seed`・`migrate` 実行ファイルでの投入・再投入、OpenAPI とハンドラ出力の一致。
- 未検証：コンテナイメージ（`/api`、`/migrate`）の実ビルド、AWS／RDS（IAM 認証）上での投入と実際のトークン発行、ECS タスクでの実行（`SEED_DATASET_IDS` を渡すインフラ設定は別途必要）、Lambda 上での `cmd/api` の起動、GitHub Actions での実行。
- `run_integration.py` の `--toolchain-report` は、PostgreSQL 17 と PostGIS を展開したディレクトリ（`postgres/root`）と同じ場所に置く、ツールチェーンの記録ファイル（JSON）です。リポジトリだけでは再現できず、同等の使い捨て環境を用意する必要があります。
- `build_dataset.py --check` が全体でバイト一致するのは、`uv.lock` で固定した環境で実行した場合です。マニフェストには実行時のライブラリ版が記録されるため、別の環境では `dataset.json` のダイジェストだけが一致します。
