# =============================================================================
# IAM — least-privilege roles for ECS Fargate (execution + task)
# =============================================================================
# Two roles per workload (ECS convention):
#   * execution role  — pulls images, writes logs, reads secrets (ECR + CW + SM)
#   * task role       — what the *app* can do at runtime (S3, Secrets, etc.)

# ── Task Execution Role (shared by api / worker / mcp) ────────────────────

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = merge(local.tags, { Name = "${local.name_prefix}-ecs-execution" })
}

# Pull from ECR + write CloudWatch Logs + fetch secrets (to inject as env vars)
resource "aws_iam_role_policy_attachment" "execution_ecr" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid    = "ReadSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "kms:Decrypt",
    ]
    resources = [
      aws_secretsmanager_secret.db_creds.arn,
      aws_secretsmanager_secret.openai.arn,
      aws_secretsmanager_secret.app_api_key.arn,
      aws_kms_key.app.arn,
    ]
  }
}

resource "aws_iam_policy" "execution_secrets" {
  name        = "${local.name_prefix}-ecs-execution-secrets"
  policy      = data.aws_iam_policy_document.execution_secrets.json
  tags        = local.tags
}

resource "aws_iam_role_policy_attachment" "execution_secrets" {
  role       = aws_iam_role.execution.name
  policy_arn = aws_iam_policy.execution_secrets.arn
}

# ── Task Role (app-level permissions at runtime) ──────────────────────────

resource "aws_iam_role" "task" {
  name               = "${local.name_prefix}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
  tags               = merge(local.tags, { Name = "${local.name_prefix}-ecs-task" })
}

data "aws_iam_policy_document" "task" {
  # S3: read/write PDFs in the pipeline bucket
  statement {
    sid    = "S3Pdfs"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      aws_s3_bucket.pdfs.arn,
      "${aws_s3_bucket.pdfs.arn}/*",
    ]
  }

  # Secrets: read DB creds + OpenAI key at runtime
  statement {
    sid    = "ReadAppSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "kms:Decrypt",
    ]
    resources = [
      aws_secretsmanager_secret.db_creds.arn,
      aws_secretsmanager_secret.openai.arn,
      aws_secretsmanager_secret.app_api_key.arn,
      aws_kms_key.app.arn,
    ]
  }

  # CloudWatch: emit custom metrics / logs from the app
  statement {
    sid    = "CloudWatchPut"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "task" {
  name        = "${local.name_prefix}-ecs-task-perms"
  policy      = data.aws_iam_policy_document.task.json
  tags        = local.tags
}

resource "aws_iam_role_policy_attachment" "task" {
  role       = aws_iam_role.task.name
  policy_arn = aws_iam_policy.task.arn
}