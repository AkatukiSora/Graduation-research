# 規制ポイントの仮データ投入（seed）

`backend/app` に埋め込んだデータを PostgreSQL（PostGIS）の `regulation_datasets` / `regulation_points` / `regulation_candidates` へ投入する仕組みです。投入は `migrate`（スキーマ適用の実行ファイル）が、環境変数で指定されたときだけ、スキーマ適用の後に行います。API の起動時には実行されません。ローカルでの確認用に、同じ処理を単独で実行する `cmd/seed` も用意しています。埋め込みデータは `synthetic-v1` / `synthetic-v2` の合成 fixture と、一時的な `nagoya-rule-test-202602-v1` です。

## synthetic-v1/v2 の性質

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

`nagoya-rule-test-202602-v1` は JARTIC 由来の正規化地点と、テスト用に生成した未検証の近傍地点 proxy 方位を含む別データセットです。`synthetic=true` は全座標が架空という意味ではありません。方向は `inferred` または `unknown` のみ、confirmed は0件、有効性は全件 `unknown` です。

## 検証（データベース不要）

```sh
cd backend/app
go run ./cmd/seed --validate-only --dataset synthetic-v1
go run ./cmd/seed --validate-only --dataset synthetic-v2
go run ./cmd/seed --validate-only --dataset nagoya-rule-test-202602-v1
```

`--dataset` を省略すると、埋め込まれたすべてのデータセットを検証します。次の項目を確認し、違反があれば理由コード付きのメッセージを表示して終了コード 1 で終了します。

- v1 は `manifest.json` / `dataset.json`、v2 は manifest counts / 全 shard の SHA-256 と件数
- ID と `raw_uid`（32 文字以内）の一意性、経度 [-180, 180]・緯度 [-90, 90] の有限値
- `synthetic-v1` / `synthetic-v2` は全ポイントの方向・有効性が unknown で、データセットが合成かつ未承認であること。rule-test は `inferred` / `unknown` のみ、confirmed=0、全 validity=unknown、`stop_to_neighbor_proxy` の根拠、JARTIC source identity と nearest candidate 整合性を確認します
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

## 名古屋 rule-test 一時データの再生成

`nagoya-rule-test-202602-v1` は本番データに置き換えるまでの一時 seed です。生成時点の規模は1,939ポイント、7,454候補で、方向は inferred 1,866件・unknown 73件、confirmed 0件です。coverage・state・unknown 理由別の件数は bundle の `REPORT.json` を参照してください。推定方向は最近傍の別地点を使う `stop_to_neighbor_proxy` で、実道路の進行方向、車両の接近方向、規制の現行性、通知安全性を示しません。`validity_status` は全件 `unknown` です。

原本 ZIP、抽出した CSV、境界 GeoJSON、notice config、全中間生成物はリポジトリ外の保護された作業領域に置きます。原本 ZIP は生成 CLI に渡して同一性を検査するため保持し、CSV は ZIP 内の `typeD.csv` を `source.csv` として作業用入力ディレクトリへ抽出します。新しい空ディレクトリを使い、生成済みディレクトリを上書きしません。例の `SOURCE_DIR` は `source.csv` と `nagoya-b1.geojson` を含み、`SOURCE_ZIP` は同じ取得原本、`NOTICE_CONFIG` は帰属表示・加工表示・規約参照・権利状態を記録した JSON です。取得日時と取得時点の規約が不明な場合は、推定値を記入せず不明と記録します。

```sh
cd backend/generation_io
uv sync --locked --all-groups
export SOURCE_DIR=/path/to/protected-input
export SOURCE_ZIP=/path/to/protected-input/typeD_aichi\(1\).zip
export NOTICE_CONFIG=/path/to/protected-input/notice-config.json
export SCRATCH_DIR=/path/to/private-scratch/nagoya-rule-test
mkdir -p "$SCRATCH_DIR"

uv run --locked python -m generation_io prepare-input \\
  --data-dir "$SOURCE_DIR" --source source.csv --boundary nagoya-b1.geojson \\
  --source-month 202602 --region-id jp:aichi:nkc-2km \\
  --manifest-out "$SOURCE_DIR/input-manifest.json" \\
  --dataset 'typeD_aichi(1).zip' --publisher '公益財団法人日本道路交通情報センター' \\
  --edition '2026年02月分' --retrieved-at '取得日時不明' \\
  --terms-reference '取得時の規約不明（現行参照はnotice configに記録）' \\
  --rights-status owner-accepted-jartic-rule-test

for run in 1 2; do
  uv run --locked python -m generation_io generate \\
    --data-dir "$SOURCE_DIR" --region-id jp:aichi:nkc-2km \\
    --output-dir "$SCRATCH_DIR/generation-$run"
  uv run --locked python -m generation_io verify \\
    --manifest "$SCRATCH_DIR/generation-$run/generation-manifest.json"
  uv run --locked python -m generation_io.rule_test_seed build \\
    --generation-manifest "$SCRATCH_DIR/generation-$run/generation-manifest.json" \\
    --source-zip "$SOURCE_ZIP" --dataset-id nagoya-rule-test-202602-v1 \\
    --notice-config "$NOTICE_CONFIG" \\
    --output-dir "$SCRATCH_DIR/bundle-$run/nagoya-rule-test-202602-v1"
  uv run --locked python -m generation_io.rule_test_seed verify \\
    --bundle "$SCRATCH_DIR/bundle-$run/nagoya-rule-test-202602-v1"
done
diff -r \\
  "$SCRATCH_DIR/generation-1" "$SCRATCH_DIR/generation-2"
diff -r \\
  "$SCRATCH_DIR/bundle-1" "$SCRATCH_DIR/bundle-2"
```

`prepare-input` は入力の観測値を記録し、`generate` は schema-3 artifact を作成します。rule-test builder は入力 manifest・境界・JARTIC 原本との同一性を再検証し、生成済み bundle の `REPORT.json` と manifest を出力します。2つの独立した出力ディレクトリ間で `diff` が終了コード0になることを確認してください。raw 原本・raw CSV は Git に追加せず、確認後も保護された作業領域から公開しないでください。

本番用データが承認・検証済みになったら、内容の置換ではなく新しい immutable dataset ID を作ります。`immutable dataset の切替・退役手順` に従って新 ID を投入して read-back し、その後に `STOP_POINT_DATASET_ID` を切り替えます。rule-test は全 reader と `SEED_DATASET_IDS` から外した後、同手順の明示的な退役承認・削除手順で撤去してください。この説明は手順のみで、本番投入・selector 変更・退役は実行しません。


## 実験専用の外部方位データセット

`external-stop-approach-test-202602-v1` は、既存候補から固定した teacher 394 地点・target 1,939 地点について、チーム内でオフライン生成した実験用推定値です。保存先は `experimental_direction_datasets` / `experimental_direction_estimates` で、`regulation_points`、`regulation_candidates`、`SEED_DATASET_IDS`、API selector から分離しています。既存 API はこれらのテーブルを読みません。migration では、既定の全テーブル SELECT 権限を打ち消すため、実行時の読み取り専用ロール `gr9app` から両テーブルの権限をすべて取り消します。

この値は外部実装 `local_stop_approach` による車両接近方位の推定で、公式な標識方位・規制現況・通知安全性を示しません。2,333 行のうち teacher は inferred 388 / unknown 6、target は inferred 1,937 / unknown 2 です。unknown 行とその理由は保持し、全行で `confidence` は `NULL`、保存時は `production_approved=false` / `data_purpose=direction_test` を固定します。外部実装の再配布ライセンスは未確認であり、その原コード、CSV、PBF、SQLite DB は含めず、チーム内で生成した値と provenance manifest のみを埋め込みます。

出典表示は、JARTIC 由来 2026年02月候補データと OpenStreetMap contributors（ODbL 1.0）です。外部方位は JARTIC/OSM が作成・確認したものではありません。90% の正解目標は達成しておらず、target の正解精度は未測定です。既存候補との方位一致は正解率を意味しません。

### 明示的な検証・投入

```sh
cd backend/app
go run ./cmd/seed --experimental-directions --validate-only --dataset external-stop-approach-test-202602-v1
go run ./cmd/seed --experimental-directions --apply --dataset external-stop-approach-test-202602-v1
```

`--experimental-directions` と dataset ID の両方が必須です。validate-only は DB に接続しません。apply は既存 CLI と同じ DB 設定を使い、1 トランザクション・dataset ID advisory lock・digest conflict 拒否・全行 read-back を行います。失敗時は transaction を rollback します。投入は使い捨ての local PostGIS でのみ実施してください。通常の `--validate-only` には実験データが含まれず、`migrate` の `SEED_DATASET_IDS` にも追加されません。本番 DB への投入、API での選択、配備・有効化はこの変更の対象外です。

## API が参照するデータセット

API（`cmd/api`）は設定 `STOP_POINT_DATASET_ID` で指定した 1 つのデータセットだけを返します。未設定時の既定値は `synthetic-v1` です。設定値は空にできず、小文字英数字と `.` `_` `-`（先頭は英数字、64 文字以内）に限られます。存在しない ID に自動 fallback はありません。range は `[]`、detail は 404 になり得ます。DB 接続の health が成功していても selector の誤りは検出されないため、切替時は実データを read-back してください。

埋め込み済みの `synthetic-v1` / `synthetic-v2` は合成 fixture、`nagoya-rule-test-202602-v1` は JARTIC の愛知県警 2026年02月分に由来する実在地点へ、最近傍地点を使ったテスト専用の仮方位を付けた一時データです。データセットの provenance (`data_purpose=rule_test`) と DB 上の根拠列で区別し、API の応答には含めません。この方位は `stop_to_neighbor_proxy` であり、実道路の進行方向・車両の接近方向・規制の現行性・通知安全性を示しません。地点の有効性は `unknown` のままです。取得日時と取得時点の規約は不明であり、現在参照する利用条件は当時の取得条件の証拠ではありません。OSM はこの方式に使用しません。API の既定 selector は引き続き `synthetic-v1` で、rule-test は設定で明示的に選択した場合だけ返します。

API range/detail は `uniqueKey`、`lat`、`long`、`az`（nullable string）、`directionStatus`（unknown / inferred / confirmed）だけを返します。有効性、方向・有効性の reason/evidence、`imputationMethod`、`directionSource`、`dataPurpose`、dataset ID と年月、作成・更新日時は応答に含めず、DB にだけ保持します。通知可否や角度しきい値は判定せず、クライアントが `directionStatus` を見て独自に判断します。候補一覧は API に出力されません。

出典・加工表示: 交通規制情報（公益財団法人日本道路交通情報センター、2026年02月分）を本プロジェクトが地域抽出・正規化し、欠損方向に近傍点 proxy を付与します。推定値は原提供者が作成・確認したものではありません。JARTIC の案内は [オープンデータ](https://www.jartic.or.jp/service/opendata/)、参照した利用条件は [利用規約](https://www.jartic.or.jp/d/opendata/riyou_kiyaku.pdf) です。原取得日時および当時の適用規約は不明で、owner の受容判断は法的保証を意味しません。この作業ではデータ bundle の生成・公開、完全な機械可読 download の提供、実環境への投入・配備は行いません。

- DB に接続できないときは `/api/v1/health` が 503（`{"status":"degraded","database":"unavailable"}`）、地点取得も 503（`error.code=database_unavailable`）を返します。
- IAM 認証では新しい接続ごとに認証 token を再生成します。`DB_PASSWORD` が設定されている場合は固定 password を使い、AWS には問い合わせません。

## immutable dataset の切替・退役手順

同じ ID の内容を置き換えず、新しい dataset ID と manifest を追加します。埋め込みデータを含む API/seed image を新しく build し、`SEED_DATASET_IDS`（投入対象）と `STOP_POINT_DATASET_ID`（API selector）を別々に設定します。実行時に任意 bundle を読み込む経路はありません。

1. 旧版を保持したまま新版を validate-only し、manifest digest、DB の dataset/point/candidate 件数、根拠件数、代表 ID を確認します。bundle、notice、manifest と全 shard を一体の immutable archive として保存してください。
2. 新 ID を `SEED_DATASET_IDS` に追加して投入し、DB read-back で件数と代表地点を確認します。各 ID の投入は個別 transaction であり、複数 ID 全体の atomicity はありません。
3. 新 ID を含む image を build し、API の `STOP_POINT_DATASET_ID` を新 ID に変更して起動します。range/detail の `uniqueKey`、`az`、`directionStatus` が新版の値であることを DB の read-back と照合し、旧版の混入がないことを確認してください（応答に dataset ID は含まれないため、selector の設定値と DB の dataset ID で確認します）。未存在 ID は自動 fallback しません。
4. rolling update 中は旧 image と新版 image の応答が一時的に混在します。全 replica の read-back が新 ID を示すまで切替完了と扱いません。問題時は旧 selector と旧 image に戻します。新 ID の投入だけでは既存 API の選択先は変わりません。
5. 旧版の退役は任意で、切替とは別の人手承認作業です。全旧 reader の停止、旧 ID の `SEED_DATASET_IDS` からの除去、backup と rollback 期間、immutable archive/notice の保管を人が確認してから実施します。削除する場合は対象 dataset ID を明示し、同 dataset の advisory transaction lock を取得して `candidates` → `points` → `datasets` の順に同 ID 条件で削除し、件数を確認します。wildcard、`TRUNCATE`、`CASCADE`、他 ID の変更、active reader がある状態での削除は行いません。旧公開 bundle と notice は保持し、seed 設定に旧 ID を残して再投入で復活させないでください。

本手順は運用説明であり、この変更では実 DB 操作、migration 適用、deploy、公開、退役を実施しません。将来 RF 版へ切り替える場合も新 ID を用いますが、RF 固有 generator/model registry と精度検証は別途必要です。

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

後者は使い捨てのローカル PostGIS クラスタを作成し、全マイグレーションを適用して、seed／API repository／接続プールの統合テストを実行します。seed では synthetic-v1/v2 の既存検証に加え、rule-test の初回投入・冪等再実行・状態／根拠件数・API repository の detail/range 読戻しを確認します。JSONL decode／gzip CRC／context cancellation／候補 INSERT 失敗時の rollback、並行投入も確認します。さらに `migrate` が v1/v2/rule-test を同じ実行で投入・再実行し、未知 ID は書き込み前に拒否することを検証し、終了時にクラスタを停止・削除します。scratch ディレクトリと read-only の toolchain report は別のパスにしてください。統合テストは `TEST_DATABASE_URL` が未設定ならスキップされ、ホストがループバックまたはローカルのソケット以外の場合は実行を拒否します。

`check_openapi.py` は、ハンドラが実際に返す JSON と `backend/docs/openapi.yaml` のプロパティ・必須項目・エラーコードの一致を確認します（データベースは不要で、PyYAML または ruamel.yaml が必要です）。

## 検証範囲

- `go test ./...` と `go vet ./...` は Go 全パッケージを対象にします。ローカル PostGIS runner は v1/v2/rule-test の seed 初回投入・再実行、migrate 経路、API repository read-back を検証します。
- v2 の10,000ポイント／60,000候補 fixture は streaming load と全 object 配列を保持しないことを確認するテスト対象です。過去の Linux 検証では process high-water RSS は45,104 KiB（512 MiB 未満）でした。
- 未検証の運用環境：コンテナイメージ（`/api`、`/migrate`）の実ビルド、AWS／RDS（IAM 認証）上での投入と実際の token 発行、ECS タスクでの実行（`SEED_DATASET_IDS` を渡すインフラ設定は別途必要）、Lambda 上での `cmd/api` 起動、GitHub Actions での実行。
- `run_integration.py` の `--toolchain-report` は PostgreSQL 17 と PostGIS を展開したディレクトリ（`postgres/root`）と同じ場所に置くツールチェーン記録 JSON です。リポジトリだけでは再現できず、同等の使い捨て環境を用意する必要があります。
- `build_dataset.py --check` が全体でバイト一致するのは `uv.lock` で固定した環境で実行した場合です。マニフェストには実行時のライブラリ版が記録されるため、別の環境では `dataset.json` の digest だけが一致します。
