output "vpc_id" {
  description = "Dev VPC ID."
  value       = module.network.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs used by the dev Aurora subnet group."
  value       = module.network.private_subnet_ids
}

output "db_security_group_id" {
  description = "Security group attached to the dev Aurora cluster."
  value       = module.network.db_security_group_id
}

output "app_security_group_id" {
  description = "Security group for app workloads that are allowed to connect to the dev Aurora cluster."
  value       = module.network.app_security_group_id
}

output "db_cluster_endpoint" {
  description = "Writer endpoint for Aurora PostgreSQL."
  value       = module.aurora_postgres.cluster_endpoint
}

output "db_cluster_reader_endpoint" {
  description = "Reader endpoint for Aurora PostgreSQL."
  value       = module.aurora_postgres.reader_endpoint
}

output "db_cluster_resource_id" {
  description = "Resource ID used for IAM DB authentication policies."
  value       = module.aurora_postgres.cluster_resource_id
}

output "db_master_user_secret_arn" {
  description = "Secrets Manager ARN for the generated master password."
  value       = module.aurora_postgres.master_user_secret_arn
}

output "db_name" {
  description = "Initial database name."
  value       = module.aurora_postgres.database_name
}

output "db_port" {
  description = "Database port."
  value       = module.aurora_postgres.port
}

output "ecr_repository_url" {
  description = "ECR repository URL for the backend application image."
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com/${var.ecr_repository_name}"
}

output "lambda_function_name" {
  description = "Lambda function name for the backend API."
  value       = module.lambda_service.function_name
}

output "http_api_endpoint" {
  description = "HTTP API endpoint for the backend API."
  value       = module.http_api.api_endpoint
}

output "ecs_cluster_name" {
  description = "ECS cluster name used for migration tasks."
  value       = module.ecs_migration.cluster_name
}

output "migration_task_definition_arn" {
  description = "Migration task definition ARN."
  value       = module.ecs_migration.task_definition_arn
}

output "migration_task_container_name" {
  description = "Migration task container name."
  value       = module.ecs_migration.container_name
}
