variable "aws_region" {
  description = "AWS region used for the dev environment."
  type        = string
  default     = "ap-northeast-3"
}

variable "aws_profile" {
  description = "Optional shared AWS profile name for local Terraform execution."
  type        = string
  default     = null
}

variable "name_prefix" {
  description = "Prefix used for resource names."
  type        = string
  default     = "gr9-dev"
}

variable "vpc_cidr" {
  description = "CIDR block for the dev VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "private_subnet_cidrs" {
  description = "Private subnet CIDRs used for the dev Aurora subnet group."
  type        = list(string)
  default     = ["10.20.1.0/24", "10.20.2.0/24"]

  validation {
    condition     = length(var.private_subnet_cidrs) == 2
    error_message = "The initial dev environment currently requires exactly 2 private subnet CIDRs."
  }
}

variable "database_name" {
  description = "Initial database name."
  type        = string
  # Dev migrations currently assume a fixed database name.
  default = "gr9_dev"
}

variable "master_username" {
  description = "Master username for Aurora PostgreSQL."
  type        = string
  default     = "gr9admin"
}

variable "engine_version" {
  description = "Aurora PostgreSQL engine version."
  type        = string
  default     = "16.11"
}

variable "serverless_min_acu" {
  description = "Minimum ACU. Use 0 only when the selected engine version supports auto-pause in the target region."
  type        = number
  default     = 0
}

variable "serverless_max_acu" {
  description = "Maximum ACU for the dev cluster."
  type        = number
  default     = 2
}

variable "seconds_until_auto_pause" {
  description = "Idle seconds before Aurora auto-pauses."
  type        = number
  default     = 900
}

variable "backup_retention_period" {
  description = "Automated backup retention in days."
  type        = number
  default     = 1
}

variable "app_image_uri" {
  description = "Container image URI used by Lambda and ECS migration task."
  type        = string
}

variable "app_db_user" {
  description = "Database username used by the application runtime."
  type        = string
  # Dev migrations currently assume a fixed application DB user.
  default = "gr9app"
}

variable "lambda_function_name" {
  description = "Lambda function name for the backend API."
  type        = string
  default     = "gr9-dev-api"
}

variable "http_api_name" {
  description = "HTTP API name for the backend API."
  type        = string
  default     = "gr9-dev-api"
}

variable "ecr_repository_name" {
  description = "ECR repository name for the backend application image."
  type        = string
  default     = "gr9-backend-app"
}

variable "lambda_memory_size" {
  description = "Lambda memory size in MB."
  type        = number
  default     = 256
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds."
  type        = number
  default     = 15
}

variable "migration_task_cpu" {
  description = "CPU units used by the migration task."
  type        = number
  default     = 256
}

variable "migration_task_memory" {
  description = "Memory in MiB used by the migration task."
  type        = number
  default     = 512
}

variable "enable_secretsmanager_endpoint" {
  description = "Whether to create a Secrets Manager VPC endpoint for the migration task."
  type        = bool
  default     = true
}

variable "enable_kms_endpoint" {
  description = "Whether to create a KMS VPC endpoint for the migration task."
  type        = bool
  default     = false
}

variable "enable_vpc_endpoints" {
  description = "Whether to create VPC endpoints during an apply."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Common tags applied to the dev environment."
  type        = map(string)
  default = {
    Project     = "gr9"
    Environment = "dev"
    ManagedBy   = "Terraform"
    Repository  = "NKC-CV2026/Graduation-research"
  }
}
