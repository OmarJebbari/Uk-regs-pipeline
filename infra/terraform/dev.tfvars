# =============================================================================
# dev.tfvars — cost-conscious dev environment
# =============================================================================
# Usage:
#   terraform apply -var-file=dev.tfvars
#
# Cost savings:
#   - single-AZ RDS (no standby)
#   - no NAT gateway (use VPC endpoints for S3/ECR)
#   - 1 API task
#   - smaller DB instance

environment            = "dev"
aws_region             = "eu-west-2"
project_name           = "uk-regs"

# ── Network ──
vpc_cidr               = "10.30.0.0/16"
availability_zones     = ["eu-west-2a", "eu-west-2b"]
enable_nat_gateway     = false

# ── Database ──
db_instance_class      = "db.t4g.small"
db_allocated_storage   = 20
db_max_allocated_storage = 50
db_multi_az            = false
db_backup_retention_days = 1

# ── ECS ──
api_cpu                = 256
api_memory             = 512
api_desired_count      = 1
log_retention_days     = 7

# ── DNS (optional in dev — ALB DNS works without it) ──
create_route53_zone    = false
domain_name            = "dev-api.ukregs.example.com"
route53_zone_name      = "ukregs.example.com"

# ── Secrets — set these via env vars, not in this file:
#   TF_VAR_openai_api_key=sk-...
#   TF_VAR_api_key=$(openssl rand -hex 32)