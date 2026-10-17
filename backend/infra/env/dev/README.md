# Terraform Dev Environment

`backend/infra/env/dev` は dev 用インフラです。現段階では次を作成します。

- VPC
- Aurora 用 private subnet x 2
- Aurora PostgreSQL Serverless v2
- IAM DB authentication を有効化した dev DB
- Lambda (container image)
- API Gateway HTTP API
- ECS/Fargate migration task
- ECR / Logs / S3 / Secrets Manager / KMS 用 VPC endpoint

## この段階の意図

- DB / API / migration task を dev 環境としてまとめて構成する
- GitHub Actions から Docker build / push 後に dev をデプロイできる状態にする
- Lambda と ECS migration task の CPU architecture は `arm64` を前提にしています

## 現時点の割り切り

- NAT Gateway は作成しません
- RDS Proxy は作成しません
- DB 初期化は Terraform に含めません
- DB schema は app 側 migration で管理する前提です
- dev の migration task は `SEED_DATASET_IDS` で指定した埋め込みデータをスキーマ適用後に投入します。Terraform 自体は DB 行を管理しません
- DB は private-only で、外部から直接接続しない前提です
- app / Lambda 用 security group は先に作成し、その SG からのみ DB へ 5432/TCP を許可します
- migration task は ECS/Fargate one-shot task を使い、同じ app イメージの `/migrate` を実行します
- ECR repository 自体は bootstrap 側で作成する前提です

## 事前準備

```bash
cd backend/infra/env/dev
cp terraform.tfvars.example terraform.tfvars
```

## 実行

```bash
terraform init
terraform plan
terraform apply
```

## GitHub Actions で使う Variables

Repository Variables に次を設定します。

- `AWS_REGION`: `ap-northeast-3`
- `BACKEND_APP_ECR_REPOSITORY_URL`: bootstrap output の ECR repository URL
- `BACKEND_APP_READY`: `true`
- `TERRAFORM_BOOTSTRAP_PLAN_ROLE_ARN`: bootstrap output の plan role ARN
- `TERRAFORM_BOOTSTRAP_APPLY_ROLE_ARN`: bootstrap output の apply role ARN
- `TERRAFORM_DEV_READY`: `true`

`TERRAFORM_DEV_READY` は dev 環境の apply を CI/CD から許可するタイミングで設定してください。

## terraform.tfvars で主に指定する値

- `app_image_uri`
- `seed_dataset_ids`: dev migration task が投入する埋め込み ID。既定は `nagoya-rule-test-202602-v1`
- `stop_point_dataset_id`: Lambda が参照する単一 ID。既定は同じ一時データ
- `app_db_user`
- `lambda_function_name`
- `http_api_name`
- `ecr_repository_name`

## 注意点

- Aurora は DB subnet group に少なくとも 2 つの AZ にまたがる subnet を要求するため、この段階では private subnet を 2 本に固定しています
- DB は外部公開しません。後続の app / Lambda から同一 VPC 内で接続する前提です
- DB への ingress は `app_security_group_id` からの 5432/TCP のみです
- private subnet には NAT を置かないため、ECR / Logs / S3 などは VPC endpoint 経由で利用します
- migration task は初回 bootstrap deadlock を避けるため、master user の password を使って接続します
- dev の既定データ `nagoya-rule-test-202602-v1` は一時的な検証用です。方向は近傍地点への未検証 proxy (`inferred`/`unknown`) であり、道路の進行方向・接近方向・現行性・通知安全性を示しません。実運転の警告判定には使わないでください
- この HTTP API は認証なしで公開されます。地点は架空ではなく、JARTIC の2026年02月分に由来します。出典・加工表示と取得時規約の不明点は `backend/app/internal/seeddata/data/nagoya-rule-test-202602-v1/README.md` に記載しています。対応するbundle一式（manifest、全shard、README、REPORT）は [公開コミット固定のZIP](https://github.com/NKC-CV2026/Graduation-research/archive/dc73f7ca7179fe958f90e5453c3b92b4adfe9346.zip) の同ディレクトリから取得できます。公開範囲を変える際には配布条件とクライアント側の表示を再確認してください
- `backend-dev.yml` は先に migration task を起動し、正常終了後に Lambda を更新します。`SEED_DATASET_IDS` は migration task にだけ、`STOP_POINT_DATASET_ID` は Lambda にだけ渡します。投入済みかどうかは `/api/v1/health` では確認できず、地点の範囲検索と詳細取得で確認します
- 本番用データに切り替えるときは新しい immutable ID を作成し、先に `seed_dataset_ids` に追加して投入・検証し、その後 `stop_point_dataset_id` を切り替えます。dev の公開後 smoke check (`backend-dev.yml` の `Verify Seeded Dev API`) は現在の名古屋一時データの座標・ID・用途を期待するため、切替 PR では同じ変更に検査対象も更新してください。旧データの削除は別途承認が必要です（`backend/app/db/seed/README.md`）
- app runtime は `gr9app` + IAM DB authentication を使う前提です
- `serverless_min_acu = 0` は Aurora PostgreSQL の engine version とリージョンの組み合わせ依存です
- もし `MinCapacity=0` が未対応なら `0.5` に上げてください
- migration 方針は `backend/app/MIGRATION.md` を参照してください

## migration 元資料

- `backend/InitialSQL`
- `backend/ImportSQL`

また CSV 元データは現状 `Prototype/app/src/assets/points.csv` にあります。
