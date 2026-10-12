# 固定データによる互換性検証

このディレクトリには、固定された研究データと保存済み成果物を照合する検証ツールを収録しています。通常の生成CLIと合成データによるテストとは別に使用します。

## 必要な環境

検証には、各ツールが参照する入力データ、参照用の候補計算コード、固定されたPython環境、承認記録が必要です。参照パスと期待するハッシュはツール内で定義されています。これらのデータはリポジトリに同梱していません。

入力や承認記録が欠けている場合、または記録されたハッシュと一致しない場合は検証に失敗します。通常のCLIや合成テストの成功は、過去の承認記録が有効であることを意味しません。

## テストの実行

参照データを備えた環境で、リポジトリのルートから次を実行します。

```sh
cd backend/generation_io
uv run --locked --no-sync python -m pytest -q tools/legacy_acceptance/tests
```

これらのテストは標準の `tests/` 収集とGitHub Actionsには含まれません。

## 検証対象

- `test_artifacts.py`：固定入力からの成果物生成、参照計算との一致、保存済み成果物の検証。
- `test_resume.py`：schema v2の正規化済みデータを使った再計算。
- `test_source.py`、`test_raw_source.py`：固定入力の抽出項目とCSV解析。
- `test_revision2_validator.py`：保存済みの承認・実行記録と参照ファイルの整合性。

通常のschema v3の生成・検証・再計算、入力解析、候補計算、プロセス監督は、標準テスト内の合成データで検証します。

## コマンド

固定入力用の入口は `legacy-generate`、`legacy-verify`、`legacy-resume` です。各コマンドの引数は次のように確認できます。

```sh
uv run --locked python -m generation_io legacy-generate --help
```

一般の外部CSVから生成する場合は、[通常の利用手順](../README.md)の `prepare-input` と `generate` を使用してください。
