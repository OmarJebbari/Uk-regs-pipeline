# =============================================================================
# UK Regs Pipeline — input variables
# =============================================================================

# ── Global ────────────────────────────────────────────────────────────────

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "eu-west-2" # London
}

variable "environment" {
  description = "Deployment environment (used in tags + naming)."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "project_name" {
  description = "Short project prefix used in every resource name."
  type        = string
  default     = "uk-regs"
}

# ── Network ───────────────────────────────────────────────────────────────

variable "vpc_cidr" {
  description = "CIDR for the VPC."
  type        = string
  default     = "10.20.0.0/16"
}

variable "availability_zones" {
  description = "AZs to spread across (need ≥ 2 for RDS Multi-AZ)."
  type        = list(string)
  default     = ["eu-west-2a", "eu-west-2b"]
}

variable "enable_nat_gateway" {
  description = "Set false in dev to save cost (private subnets won't have outbound internet)."
  type        = bool
  default     = true
}

# ── Database (RDS Postgres + pgvector) ────────────────────────────────────

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.medium" # 2 vCPU / 4 GiB — good for pgvector at small scale
}

variable "db_allocated_storage" {
  description = "Initial storage (GiB)."
  type        = number
  default     = 50
}

variable "db_max_allocated_storage" {
  description = "Upper bound for storage autoscaling (GiB)."
  type        = number
  default     = 200
}

variable "db_multi_az" {
  description = "Enable RDS Multi-AZ (failover standby). Disable in dev."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Point-in-time recovery retention (0–35)."
  type        = number
  default     = 14
}

variable "db_name" {
  description = "Initial Postgres database name."
  type        = string
  default     = "ukregs"
}

variable "db_username" {
  description = "Master DB username."
  type        = string
  default     = "ukregs"
}

# ── S3 (object store for PDFs) ────────────────────────────────────────────

variable "s3_pdf_prefix" {
  description = "Key prefix under which original PDFs are stored."
  type        = string
  default     = "pdfs/"
}

# ── ECS Fargate (API service) ─────────────────────────────────────────────

variable "api_cpu" {
  description = "CPU units for the API task (1024 = 1 vCPU)."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "Memory (MiB) for the API task."
  type        = number
  default     = 1024
}

variable "api_desired_count" {
  description = "Number of running API tasks."
  type        = number
  default     = 2
}

variable "api_health_check_path" {
  description = "HTTP path the ALB uses for health checks."
  type        = string
  default     = "/healthz"
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 30
}

# ── DNS / TLS (Route 53 + ACM) ────────────────────────────────────────────

variable "domain_name" {
  description = "Public domain for the API (e.g. api.example.com). Required for HTTPS."
  type        = string
  default     = "api.ukregs.example.com"
}

variable "create_route53_zone" {
  description = "Set true to create + manage the hosted zone here. False = use existing zone."
  type        = bool
  default     = true
}

variable "route53_zone_name" {
  description = "Hosted zone name (parent of domain_name). Used when create_route53_zone=true."
  type        = string
  default     = "ukregs.example.com"
}

# ── Secrets (bootstrap values — leave blank in tfvars, set in AWS) ────────

variable "openai_api_key" {
  description = "OpenAI API key. Leave empty to have Secrets Manager generate a placeholder."
  type        = string
  default     = ""
  sensitive   = true
}

variable "api_key" {
  description = "API key clients must send as X-API-Key. Leave empty to auto-generate."
  type        = string
  default     = ""
  sensitive   = true
}