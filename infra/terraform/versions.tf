# =============================================================================
# UK Regs Pipeline — Terraform provider & version pinning
#
# Provisions the production AWS stack:
#   * VPC (public + private subnets, NAT)
#   * RDS PostgreSQL 16 + pgvector  (rules, vectors, change-sets)
#   * S3                              (immutable PDF originals)
#   * Secrets Manager                 (API key, OpenAI key, DB creds)
#   * ECR                             (api / worker / mcp images)
#   * ECS Fargate                     (long-running API + on-demand worker)
#   * IAM                             (least-privilege task roles)
# =============================================================================

terraform {
  required_version = ">= 1.7.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # ── Remote state ────────────────────────────────────────────────────────
  # Bootstrapping: create the S3 bucket + DynamoDB table manually once, then
  # uncomment this block. NEVER commit a `terraform.tfstate` with secrets.
  #
  # backend "s3" {
  #   bucket         = "uk-regs-terraform-state"
  #   key            = "infra/terraform.tfstate"
  #   region         = "eu-west-2"
  #   dynamodb_table = "uk-regs-terraform-locks"
  #   encrypt        = true
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "uk-regs-pipeline"
      Environment = var.environment
      ManagedBy   = "terraform"
      Repo        = "OmarJebbari/Uk-regs-pipeline"
    }
  }
}

# =============================================================================
# Shared naming prefix — keeps every resource consistently + uniquely named.
# All resources use `local.name_prefix` so dev/prod never collide.
# =============================================================================
locals {
  name_prefix = "${var.project_name}-${var.environment}"

  # Named `tags` so every resource can call `merge(local.tags, { Name = ... })`
  # and the provider's `default_tags` stays as the explicit block above.
  tags = {
    Project     = "uk-regs-pipeline"
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}
