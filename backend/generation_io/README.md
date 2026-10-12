# 一時停止規制の候補データ生成

`generation_io` は、規制CSVから指定地域の一時停止規制を抽出し、近隣点との距離・方位などの特徴量を持つ候補データを生成するCLIです。入力の検査、成果物の検証、候補計算の再実行に対応しています。

実際の規制CSVと境界データは利用者が用意します。リポジトリにはテスト用の合成データだけを同梱しています。

## セットアップ

Linux、Python 3.13.5、[uv](https://docs.astral.sh/uv/) を使用します。リポジトリのルートから次を実行してください。

```sh
cd backend/generation_io
uv sync --locked --all-groups
uv run --locked python -m generation_io --help
```

以降のコマンドも `backend/generation_io` で実行します。

## 入力データ

次の2ファイルを、出力先とは別のディレクトリに配置します。

```text
external-data/
├── source.csv
└── boundary.geojson
```

- **規制CSV**：CP932、拡張版標準フォーマット `k_2.1`、対応する170列のヘッダー。
- **境界GeoJSON**：PolygonまたはMultiPolygonを持つ単一のFeature。座標はWGS84の経度・緯度。FeatureCollectionや座標変換には対応していません。

データの取得元と利用条件を確認してから使用してください。CSVと境界ファイルは読取り専用で扱います。

### 入力マニフェストの作成

入力マニフェストは、ファイルのサイズ・ハッシュ、対象月、地域識別子、出典情報を記録するJSONです。

```sh
uv run --locked python -m generation_io prepare-input \
  --data-dir ./external-data \
  --source source.csv \
  --boundary boundary.geojson \
  --source-month 202602 \
  --region-id jp:aichi:nagoya \
  --manifest-out ./external-data/input-manifest.json
```

対象月と地域識別子は、用意したデータに合わせて指定します。`--source` と `--boundary` は入力ディレクトリからの相対パスです。絶対パス、親ディレクトリへの参照、シンボリックリンクは受け付けません。

出典情報は `--dataset`、`--publisher`、`--edition`、`--retrieved-at`、`--terms-reference`、`--rights-status` で指定できます。省略した情報は `unknown` として記録します。合成データの場合は `--synthetic` を指定します。

`prepare-input` は指定先にマニフェストを新規作成し、既存ファイルを上書きしません。ハッシュは検査時点のファイルとの一致を確認するためのもので、提供元の真正性や利用許諾を証明するものではありません。

## 生成・検証・再計算

```sh
uv run --locked python -m generation_io generate \
  --data-dir ./external-data \
  --input-manifest input-manifest.json \
  --region-id jp:aichi:nagoya \
  --output-dir ./run-001

uv run --locked python -m generation_io verify \
  --manifest ./run-001/generation-manifest.json

uv run --locked python -m generation_io resume \
  --manifest ./run-001/generation-manifest.json \
  --output-dir ./run-002
```

`generate` は入力を検査して地域内の候補を生成します。出力先は未作成のディレクトリを指定してください。既存の空ディレクトリも使用できません。

`verify` は成果物の形式、件数、サイズ、ハッシュを検査します。元CSVや境界データは不要です。成果物ディレクトリを移動した場合も、移動先のマニフェストを指定して検証できます。

`resume` は検証済みの正規化データから候補特徴量を再計算します。CSVの再読込みや地域抽出は行わず、元の成果物を変更しません。再計算先は別の新規ディレクトリを指定します。再計算後も `verify` で検証できます。

### 出力ファイル

- `normalized.jsonl`：地域内の正規化済み規制レコード。
- `rejected.jsonl`：候補にできなかったレコードと理由。
- `candidates.jsonl`：近隣点との距離・方位などの候補特徴量。
- `source-manifest.json`：入力の参照情報と出典。
- `generation-manifest.json`：成果物のサイズ・ハッシュ・件数・計算条件。

成功時は標準出力にJSONを返します。`status` は生成・再計算で `complete`、検証で `verified` です。候補が0件でも正常終了です。失敗時は標準エラー出力にエラー情報を返し、終了コードは `2` になります。

## 時間・メモリを監視して実行する

通常のCLIは成果物を直接生成します。実行時間やメモリを監視する場合は `generation_io.supervisor` を使用します。入力、成果物、実行状態のディレクトリは互いに独立した場所を指定してください。

```sh
uv run --locked python -m generation_io.supervisor \
  --init-attempt --state-dir ./supervisor-state

uv run --locked python -m generation_io.supervisor \
  --stage generate \
  --state-dir ./supervisor-state \
  --data-dir ./external-data \
  --input-manifest input-manifest.json \
  --region-id jp:aichi:nagoya \
  --output-dir ./supervised-run-001

uv run --locked python -m generation_io.supervisor \
  --stage verify \
  --state-dir ./supervisor-state \
  --manifest ./supervised-run-001/generation-manifest.json
```

再計算は `--stage resume --manifest ... --output-dir ...` で実行できます。

実行状態の初期化は一度だけ行います。既存の `budget-state.json` は上書きされません。同じ実行状態を使う処理の消費時間は累積して記録され、残予算がない場合は次の処理を起動しません。

実行状態には各処理のログ・監視結果・出力の所有権記録を保存します。子プロセスの終了を確認できない場合は未完了状態を保持し、後続処理を拒否します。検証対象や所有権を確認できない出力は削除しません。

### 実行上限

- 子プロセスのRSS：512 MiB。開始・実行時に必要なメモリとディスクの空き：各1 GiB。
- 成果物の合計：32 MiB。生成マニフェスト等のレポート合計：8 MiB。
- 監視レポート・ログ：8 MiB。
- 監督実行の生成・再計算：各120秒。検証：60秒。
- 同じ実行状態の累積予算：600秒。実行状態の有効時間：初期化から3600秒。

CSVは逐次読み込みますが、候補計算では地域内の正規化済み点をメモリに保持します。上記は入力CSVのサイズ上限ではありません。処理終了・回収にかかった時間も累積予算へ記録します。

## データの意味と利用上の注意

規制種別コード `63` を候補対象とし、その他の規制は除外します。更新区分が空でない行は未対応として記録します。更新区分の意味や規制の現在の有効性は判定しません。

候補の方位は点間の幾何的な方向です。標識の向き、道路の進行方向、安全な進入方向を保証するものではありません。成果物には `production_approved: false` を記録します。

信頼できるローカル環境で使用してください。検査中に第三者が入力やディレクトリを変更する環境での安全性は保証していません。

## テスト

```sh
uv lock --check
uv sync --locked --all-groups
uv run --locked --no-sync python -m pytest -p no:cacheprovider -q
```

標準テストは合成データと一時ディレクトリを使用し、実際の規制情報を必要としません。GitHub Actionsでも同じテストをLinux・Python 3.13.5で実行します。

固定された研究データを使用する検証ツールと `legacy-*` コマンドについては、[検証ツールの説明](tools/README.md)を参照してください。
