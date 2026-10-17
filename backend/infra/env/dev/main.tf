module "network" {
  source = "../../modules/network"

  name_prefix          = var.name_prefix
  vpc_cidr             = var.vpc_cidr
  availability_zones   = local.availability_zones
  private_subnet_cidrs = var.private_subnet_cidrs
  tags                 = var.tags
}

module "aurora_postgres" {
  source = "../../modules/aurora_postgres"

  name_prefix                = var.name_prefix
  database_name              = var.database_name
  master_username            = var.master_username
  subnet_ids                 = module.network.private_subnet_ids
  vpc_security_group_ids     = [module.network.db_security_group_id]
  allowed_security_group_ids = [module.network.app_security_group_id]
  engine_version             = var.engine_version
  serverless_min_acu         = var.serverless_min_acu
  serverless_max_acu         = var.serverless_max_acu
  seconds_until_auto_pause   = var.seconds_until_auto_pause
  backup_retention_period    = var.backup_retention_period
  tags                       = var.tags
}

module "vpc_endpoints" {
  source = "../../modules/vpc_endpoints"

  name_prefix                = var.name_prefix
  vpc_id                     = module.network.vpc_id
  region                     = var.aws_region
  subnet_ids                 = module.network.private_subnet_ids
  route_table_ids            = module.network.private_route_table_ids
  allowed_security_group_ids = [module.network.app_security_group_id]
  enable_vpc_endpoints       = var.enable_vpc_endpoints
  enable_secretsmanager      = var.enable_secretsmanager_endpoint
  enable_kms                 = var.enable_kms_endpoint
  tags                       = var.tags
}

module "lambda_service" {
  source = "../../modules/lambda_service"

  name                   = var.lambda_function_name
  image_uri              = var.app_image_uri
  subnet_ids             = module.network.private_subnet_ids
  security_group_ids     = [module.network.app_security_group_id]
  memory_size            = var.lambda_memory_size
  timeout                = var.lambda_timeout
  region                 = var.aws_region
  account_id             = data.aws_caller_identity.current.account_id
  db_cluster_resource_id = module.aurora_postgres.cluster_resource_id
  db_user                = var.app_db_user
  environment_variables = {
    DB_HOST               = module.aurora_postgres.cluster_endpoint
    DB_PORT               = tostring(module.aurora_postgres.port)
    DB_NAME               = module.aurora_postgres.database_name
    DB_USER               = var.app_db_user
    DB_IAM_AUTH           = "true"
    DB_SSL_MODE           = "require"
    STOP_POINT_DATASET_ID = var.stop_point_dataset_id
  }
  tags = var.tags
}

module "http_api" {
  source = "../../modules/http_api"

  name                 = var.http_api_name
  lambda_function_name = module.lambda_service.function_name
  lambda_invoke_arn    = module.lambda_service.invoke_arn
  tags                 = var.tags
}

module "ecs_migration" {
  source = "../../modules/ecs_migration"

  name_prefix            = var.name_prefix
  image_uri              = var.app_image_uri
  region                 = var.aws_region
  account_id             = data.aws_caller_identity.current.account_id
  subnet_ids             = module.network.private_subnet_ids
  security_group_ids     = [module.network.app_security_group_id]
  db_cluster_resource_id = module.aurora_postgres.cluster_resource_id
  app_db_user            = var.app_db_user
  db_secret_arn          = module.aurora_postgres.master_user_secret_arn
  cpu                    = var.migration_task_cpu
  memory                 = var.migration_task_memory
  environment_variables = {
    AWS_REGION          = var.aws_region
    DB_HOST             = module.aurora_postgres.cluster_endpoint
    DB_PORT             = tostring(module.aurora_postgres.port)
    DB_NAME             = module.aurora_postgres.database_name
    DB_USER             = var.master_username
    DB_IAM_AUTH         = "false"
    DB_SSL_MODE         = "require"
    ATLAS_BINARY_PATH   = "/usr/local/bin/atlas"
    ATLAS_MIGRATION_DIR = "file:///app/db/migrations"
    SEED_DATASET_IDS    = var.stop_point_dataset_id
  }
  tags = var.tags
}
