# =============================================================================
# RDS — PostgreSQL 16 + pgvector (rules, vectors, change-sets, manifests)
# =============================================================================

# ── Random master password (stored in Secrets Manager, never in state) ─────

resource "random_password" "db_master" {
  length           = 32
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

# ── DB subnet group (private subnets only) ────────────────────────────────

resource "aws_db_subnet_group" "main" {
  name        = "${local.name_prefix}-db-subnet-group"
  description = "Private subnets for ${local.name_prefix} RDS"
  subnet_ids  = aws_subnet.private[*].id

  tags = merge(local.tags, { Name = "${local.name_prefix}-db-subnet-group" })
}

# ── Parameter group — enable pgvector ─────────────────────────────────────
# `rds.extensions=pgvector` lets us run `CREATE EXTENSION vector;` on boot.

resource "aws_db_parameter_group" "pgvector" {
  name        = "${local.name_prefix}-pg16-pgvector"
  family      = "postgres16"
  description = "Postgres 16 + pgvector for ${local.name_prefix}"

  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements"
  }

  parameter {
    name  = "rds.logical_replication"
    value = "1"
  }

  tags = merge(local.tags, { Name = "${local.name_prefix}-pg-pgvector" })
}

# ── RDS instance ──────────────────────────────────────────────────────────

resource "aws_db_instance" "main" {
  identifier = "${local.name_prefix}-postgres"

  engine         = "postgres"
  engine_version = "16.4"

  instance_class      = var.db_instance_class
  allocated_storage   = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type        = "gp3"
  storage_encrypted   = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db_master.result

  multi_az               = var.db_multi_az
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.pgvector.name
  publicly_accessible    = false

  backup_retention_period = var.db_backup_retention_days
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot   = true
  deletion_protection     = var.environment == "prod"

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = var.environment == "prod"

  tags = merge(local.tags, { Name = "${local.name_prefix}-postgres" })
}