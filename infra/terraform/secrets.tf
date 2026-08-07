# =============================================================================
# Secrets Manager — DB credentials, OpenAI key, API key
# =============================================================================
# Secrets are NEVER stored in plaintext in terraform.tfstate. We store them as
# JSON in Secrets Manager and inject into ECS tasks via `valueFrom` (ARN refs).

# ── DB credentials (consumed by the API/worker via DATABASE_URL) ───────────

resource "aws_secretsmanager_secret" "db_creds" {
  name        = "${local.name_prefix}/db/credentials"
  description = "RDS master credentials for ${local.name_prefix}"
  kms_key_id  = aws_kms_key.app.arn

  recovery_window_in_days = 30

  tags = merge(local.tags, { Name = "${local.name_prefix}-db-secret" })
}

resource "aws_secretsmanager_secret_version" "db_creds" {
  secret_id = aws_secretsmanager_secret.db_creds.id

  secret_string = jsonencode({
    username = var.db_username
    password = random_password.db_master.result
    engine   = "postgres"
    host     = aws_db_instance.main.address
    port     = 5432
    dbname   = var.db_name
    dbInstanceIdentifier = aws_db_instance.main.identifier
  })
}

# ── OpenAI API key ────────────────────────────────────────────────────────

resource "random_uuid" "api_key" {
  count = var.api_key == "" ? 1 : 0
}

resource "aws_secretsmanager_secret" "openai" {
  name        = "${local.name_prefix}/openai/api-key"
  description = "OpenAI API key for LLM extraction + embeddings"
  kms_key_id  = aws_kms_key.app.arn

  tags = merge(local.tags, { Name = "${local.name_prefix}-openai-secret" })
}

resource "aws_secretsmanager_secret_version" "openai" {
  secret_id = aws_secretsmanager_secret.openai.id

  secret_string = jsonencode({
    api_key = var.openai_api_key == "" ? "REPLACE_ME_IN_AWS_CONSOLE" : var.openai_api_key
  })
}

# ── App API key (X-API-Key header) ────────────────────────────────────────

resource "aws_secretsmanager_secret" "app_api_key" {
  name        = "${local.name_prefix}/api/key"
  description = "API key clients must send as X-API-Key"
  kms_key_id  = aws_kms_key.app.arn

  tags = merge(local.tags, { Name = "${local.name_prefix}-apikey-secret" })
}

resource "aws_secretsmanager_secret_version" "app_api_key" {
  secret_id = aws_secretsmanager_secret.app_api_key.id

  secret_string = jsonencode({
    api_key = var.api_key == "" ? random_uuid.api_key[0].result : var.api_key
  })
}

# ── KMS key for envelope encryption of secrets + ECR scan res ─────────────

resource "aws_kms_key" "app" {
  description             = "KMS key for ${local.name_prefix} secrets + logs"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = merge(local.tags, { Name = "${local.name_prefix}-kms" })
}

resource "aws_kms_alias" "app" {
  name          = "alias/${local.name_prefix}"
  target_key_id = aws_kms_key.app.key_id
}