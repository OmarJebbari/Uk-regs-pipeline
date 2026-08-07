# =============================================================================
# prod.tfvars — production-grade, HA, durable
# =============================================================================
# Usage:
#   terraform apply -var-file=prod.tfvars
#
# Production hardening:
#   - Multi-AZ RDS with 35-day PITR backups
#   - NAT gateway (private egress)
#   - 3 API tasks (rolling deploys, no downtime)
#   - 14-day CloudWatch retention
#   - RDS Performance Insights on

environment            = "prod"
aws_region             = "eu-west-2"
project_name           = "uk-regs"

# ── Network ──
vpc_cidr               = "10.20.0.0/16"
availability_zones     = ["eu-west-2a", "eu-west-2b", "eu-west-2c"]
enable_nat_gateway     = true

# ── Database ──
db_instance_class      = "db.r6g.large"   # memory-optimized for pgvector HNSW
db_allocated_storage   = 100
db_max_allocated_storage = 500
db_multi_az            = true
db_backup_retention_days = 35

# ── ECS ──
api_cpu                = 1024            # 1 vCPU per task
api_memory             = 2048
api_desired_count      = 3
log_retention_days     = 14

# ── DNS ──
create_route53_zone    = true
domain_name            = "api.ukregs.example.com"
route53_zone_name      = "ukregs.example.com"

# ── Secrets — set these via env vars, not in this file:
#   TF_VAR_openai_api_key=sk-...
#   TF_VAR_api_key=$(openssl rand -hex 32)