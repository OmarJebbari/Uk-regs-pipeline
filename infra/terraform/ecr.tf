# =============================================================================
# ECR — container image registries (api / worker / mcp)
# =============================================================================

locals {
  ecr_repos = ["api", "worker", "mcp"]
}

resource "aws_ecr_repository" "main" {
  for_each = toset(local.ecr_repos)

  name                 = "${local.name_prefix}/${each.key}"
  image_tag_mutability = "MUTABLE" # dev convenience; flip to IMMUTABLE in prod-pure
  force_delete         = var.environment != "prod"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.app.arn
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = merge(local.tags, { Name = "${local.name_prefix}-${each.key}" })
}

# ── Lifecycle: keep last 10 tagged + 7 days of untagged ────────────────────
resource "aws_ecr_lifecycle_policy" "main" {
  for_each = aws_ecr_repository.main

  repository_name = each.value.name

  policy = jsonencode({
    rules = [
      {
        ruleId = "expire-untagged"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 7
        }
        action = { type = "expire" }
      },
      {
        ruleId = "keep-last-10-tagged"
        selection = {
          tagStatus      = "tagged"
          tagPrefixList  = ["v", "sha-", "latest"]
          countType      = "imageCountMoreThan"
          countNumber    = 10
        }
        action = { type = "expire" }
      }
    ]
  })
}