# Migration Plan

DB schema の初期化と更新は Terraform ではなく app 側で管理します。

## 方針

- app イメージに migration 実行機能を組み込みます
- 初回 schema init も migration として管理します
- 将来的な migration job でも app イメージを流用します
- DB を外部公開して手動で SQL を流す運用は前提にしません
- schema 管理は `atlas`、アプリケーションクエリ生成は `sqlc` を利用します

## 既存 SQL の扱い

以下の既存 SQL は migration 作成時の元資料として扱います。

- `backend/InitialSQL`
- `backend/ImportSQL`

## 最初に必要な migration

少なくとも次の内容を migration 化します。

1. `postgis` extension の有効化
2. `stop_signs` テーブル作成
3. `geom geography(Point, 4326)` の定義
4. `gist` index 作成

`ImportSQL` の CSV 取り込みは schema migration と責務が異なるため、初期段階では seed / import 処理として分けて扱う想定です。

## 管理対象の分離

- `db/schema`: 現在の schema 定義
- `db/migrations`: Atlas で適用する migration 履歴
- `db/queries`: sqlc が参照するアプリケーションクエリ
- `internal/repository`: pgx と sqlc を使った DB アクセス実装

## 今後の実行形態

- app 本体デプロイ前に migration を実行
- もしくは migration 用の one-shot job を app イメージから起動
- DB 認証は app 実行ロール経由で行い、長期パスワードを app に持たせません
- migration 実行のために DB を外部公開する前提にはしません
- seed / import は migration とは分けて、将来は同じ app イメージの別実行経路で扱います

## 認証の前提

- app 本体は `gr9app` と IAM DB authentication を使います
- migration task は初回 bootstrap deadlock を避けるため、master user の password を使って接続します
- その master password は Aurora が Secrets Manager に保持するものだけを利用します
- migration 内で `gr9app` を作成し、`rds_iam` を付与する前提です

## Evidence schema expand（S1）

`202610140001_regulation_point_evidence.sql` は `regulation_points` に `imputation_method`、`imputation_reason`、`direction_source`、`direction_evidence`、`validity_reason`、`validity_evidence` を追加します。既存 unknown は `none / legacy_unrecorded / unknown / {}` とし、synthetic dataset の unknown validity だけ `synthetic_fixture / {}` にします。既存 known の方向・status・validity は変更せず、未記録の根拠を `legacy_unrecorded` として一時互換CHECKで保持します。

S1では既存の `production_approved` 列とCHECKを維持し、旧seed/APIが動作するexpand段階を保ちます。consumer移行後の列削除と最終状態CHECKは別のcontract migration（S2）の責務です。S1 migrationを実DBへ適用したり、旧writer停止・backup・deployを判断したりする手順ではありません。
