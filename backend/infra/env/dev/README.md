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
- app runtime は `gr9app` + IAM DB authentication を使う前提です
- `serverless_min_acu = 0` は Aurora PostgreSQL の engine version とリージョンの組み合わせ依存です
- もし `MinCapacity=0` が未対応なら `0.5` に上げてください
- migration 方針は `backend/app/MIGRATION.md` を参照してください

## migration 元資料

- `backend/InitialSQL`
- `backend/ImportSQL`

また CSV 元データは現状 `Prototype/app/src/assets/points.csv` にあります。
