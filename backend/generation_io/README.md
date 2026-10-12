# generation_io: portable candidate artifacts

`generation_io` は外部CSVと境界GeoJSONをread-onlyで検査し、候補専用artifactを生成・検証するCLIです。DB/APIや監督CLIは含みません。日本語のエラーはstderrへJSONで出力し、処理失敗時はexit code `2` です。

## セットアップ

Linux / Python `3.13.5` を対象にしています。lockfileに固定された依存関係と全dependency groupを使います。

```sh
uv sync --locked --all-groups
uv run --locked python -m generation_io --help
uv run --locked python -m generation_io generate --help
uv run --locked python -m generation_io prepare-input --help
```

このリポジトリには入力データを同梱していません。実行前に、利用者が権利・利用条件を確認した外部入力を配置してください。CLIはデータを取得せず、権利を承認せず、入力ファイルを編集しません。

## 入力を配置する

例では入力directoryを`./external-data`とします。`source.csv`はCP932のk_2.1 CSV、`boundary.geojson`は対応するPolygonまたはMultiPolygonです。入力manifestは、外部ファイルの検査後に別ファイルとして明示的に作成します。

```text
external-data/
├── source.csv             # 利用者が配置。CLIはread-only
└── boundary.geojson       # 利用者が配置。CLIはread-only
```

```sh
uv run --locked python -m generation_io prepare-input \
  --data-dir ./external-data \
  --source source.csv \
  --boundary boundary.geojson \
  --source-month 202701 \
  --region-id example:region \
  --manifest-out ./external-data/input-manifest.json \
  --dataset unknown \
  --publisher unknown \
  --edition unknown \
  --retrieved-at unknown \
  --terms-reference unknown \
  --rights-status unknown
```

`--source`と`--boundary`は`--data-dir`からの相対pathです。absolute path、`..` traversal、symlink、CSV/GeoJSON以外の拡張子、未対応header/format/month/CRSは拒否します。初回のCSV対応範囲はCP932・k_2.1・170列headerに限られます。GeoJSONのgeometryとCRSは`region.load_boundary`で検査します。作成先の親directoryはあらかじめ作成してください。

`--manifest-out`は入力CSV/境界とは別のpathを指定します。入力directory内への明示作成も可能ですが、manifestは`O_EXCL`相当のexclusive createで保存し、既存fileは上書きしません。入力CSVと境界は変更されません。`dataset`、`publisher`、`edition`、`retrieved_at`、`terms_reference`、`rights_status`は利用者が指定した文字列を保持します。省略時はすべて`unknown`で、`rights_status`が`approved`等に自動設定されることはありません。値を入力しても権利判断・承認を意味しません。

`--synthetic`は利用者が合成入力と申告する場合だけ指定します。省略時は`synthetic: false`です。どちらの場合も`production_approved: false`です。manifestとCLI JSONのsize/SHA-256/CRC32は検査時点で観測したbyte integrity値であり、真正性・出所・利用権の証明ではありません。

## generate → verify → resume → verify

生成outputは新しいdirectoryである必要があります。既存pathは空directoryでも拒否されます。output/stateは入力directoryと分離してください。`--output`は`--output-dir`と同じ意味の互換aliasです。

```sh
uv run --locked python -m generation_io generate \
  --data-dir ./external-data \
  --input-manifest input-manifest.json \
  --region-id example:region \
  --output-dir ./run-001

uv run --locked python -m generation_io verify \
  --manifest ./run-001/generation-manifest.json

uv run --locked python -m generation_io resume \
  --manifest ./run-001/generation-manifest.json \
  --output-dir ./run-002

uv run --locked python -m generation_io verify \
  --manifest ./run-002/generation-manifest.json
```

`generate`は`input-manifest.json`とsource/boundaryをdata-dir内からread-onlyで検査し、portable schema 3 artifactを作成します。`verify`はartifactとgeneration manifestだけを使い、元CSV・境界なしで実行できます。artifact directoryを移動した後も、移動先の`generation-manifest.json`を指定すれば検証できます。`resume`は検証済みstateからkernel部分を再計算するもので、raw CSVの再読込やregion clippingの再実行ではありません。resume元と別の新規output directoryを指定してください。

成功時のJSONは`status: complete`（generate/resume）または`status: verified`（verify）、`schema_version: 3`、`production_approved: false`を明示します。候補が0件でも正常な完了です。candidate bearingは候補点同士の幾何上のbearingに限られ、道路の進行方向・規制の公式方向・現場判断を示しません。

この実装では規制種別コード`63`を候補対象として扱い、それ以外は除外します。更新区分が空でない行は現状のportable処理ではunsupportedとしてrejectし、更新の意味や現行性を推測しません。正規化結果も候補専用であり、production承認や規制の有効性を表しません。

固定selected-projection研究用APIのCLI入口は`legacy-generate`、`legacy-verify`、`legacy-resume`として分離しています。portable `generate`はlegacy ownership receiptを要求しません。旧引数を使うresearch callerは明示的に`legacy-*`へ切り替える必要があります。これらlegacyコマンドは固定研究contractに依存し、clone利用者向けportable手順ではありません。

### 出力上限

- `32 MiB`: 1つのgeneration artifactに含まれるartifact/report等のシリアライズ済み出力の合計上限。
- `8 MiB`: source manifestとgeneration manifest等のreport合計上限。

これは生成物/reportの上限です。入力CSVサイズの上限を意味するものではありません。入力CSVは全件をメモリへ読み込まずstreamingで処理します。

## 合成テストとCI

リポジトリrootからpackage directoryへ移動し、lockfileにあるすべてのgroupを同期します。

```sh
cd backend/generation_io
uv --version
uv lock --check
uv sync --locked --all-groups
uv run --locked --no-sync python -m generation_io --help
uv run --locked --no-sync python -m pytest -p no:cacheprovider -q
```

`pyproject.toml`の標準pytest収集先は`tests/`です。標準suiteはtracked synthetic fixtureと一時directoryだけを使い、real/raw regulatory data、G01/G02 evidence、P02/P05 sibling checkoutやruntimeを読みません。supervisor testsも一時resource directoryを使いますが、production resource capsや厳密なtiming assertionは変更していません。`.github/workflows/generation-io.yml`はpull requestとmainへのpushでこのlock/test手順をLinux・Python `3.13.5`上で実行し、realdata/evidenceを使いません。

固定fixtureや過去operator acceptance依存のtestは、内容を保持したまま`tools/legacy_acceptance/tests/`へ移動しました。移動理由とportable synthetic coverage map、歴史receiptの制約は[`tools/README.md`](tools/README.md)を参照してください。これらはstandard collectionやCIに含まれず、skip/xfailで成功扱いもしません。歴史testを明示実行する場合は、READMEに記載した元の外部read-only evidence環境が必要です。

D6のfresh isolated snapshotでは`uv lock --check` / `uv sync --locked --all-groups`が成功し、import pathがsnapshot内の新しい`.venv`と`generation_io`を指すことを確認しました。全suiteは初回`127 passed, 1 failed`（独立supervisor processの120ms stageでchild marker未生成）、コード・上限を変えない再実行は`128 passed, 0 failed, 0 skipped in 109.52s`でした。後続の旧D6 6ケース個別実行は`5 passed, 1 failed`で、同じ120msケースは単独再実行ではpassしています。このtiming結果は非決定的であり、安定した全6件passとは扱いません。test capやpublic limitは変更していません。
## 監督手順

通常の`python -m generation_io`（generate/verify/resume）はportable artifact CLIであり、単独ではプロセス監督・共有budget・child process-group cleanupを行いません。運用時は既存supervisorを使い、利用者が書込み可能なstate directoryを入力directoryおよび各artifact directoryと重ならない場所に用意してください。親directoryは事前に作成します。監督CLIは入力を取得・編集せず、既存artifactを上書きしません。

```sh
uv run --locked python -m generation_io.supervisor \
  --init-attempt --state-dir ./supervisor-state

uv run --locked python -m generation_io.supervisor \
  --stage generate \
  --state-dir ./supervisor-state \
  --data-dir ./external-data \
  --input-manifest input-manifest.json \
  --region-id example:region \
  --output-dir ./run-001

uv run --locked python -m generation_io.supervisor \
  --stage verify \
  --state-dir ./supervisor-state \
  --manifest ./run-001/generation-manifest.json

uv run --locked python -m generation_io.supervisor \
  --stage resume \
  --state-dir ./supervisor-state \
  --manifest ./run-001/generation-manifest.json \
  --output-dir ./run-002

uv run --locked python -m generation_io.supervisor \
  --stage verify \
  --state-dir ./supervisor-state \
  --manifest ./run-002/generation-manifest.json
```

初期化はexclusive createで、既存の`budget-state.json`を上書き・resetしません。state directoryとdata/output/verify対象artifactはcanonical pathで分離し、symlinkや相互の親子重複を拒否します。各stageのlog、report、ownership receiptはstate directory内の新しい`attempts/attempt-<id>/`に保存されます。verifyはread-onlyでreceiptを作らず、generate/resume失敗後のcomplete manifest撤回は一致するownership/published receiptがある場合だけ行います。

監督budgetは固定値です: RSS 512 MiB、memory/disk headroom各1 GiB、artifact output 32 MiB、report/log 8 MiB、共有aggregate 600秒、task 3600秒、generate/resume各120秒、verify 60秒です。CLIから上限を引き上げるoptionはありません。旧`--attempt-state`は固定G02 evidence用のlegacy入口であり、portable利用では`--state-dir`を使用してください。
