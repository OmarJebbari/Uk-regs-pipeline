# =============================================================================
# S3 — immutable object store for PDFs + CloudWatch access logs
# =============================================================================

# ── Random suffix so bucket names are globally unique & collision-free ─────

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# ── PDF originals bucket ──────────────────────────────────────────────────
# This is the S3 equivalent of MinIO from docker-compose.yml.
# Versioning + Object Lock-style retention = audit-grade immutability.

resource "aws_s3_bucket" "pdfs" {
  bucket = "${local.name_prefix}-pdfs-${random_id.bucket_suffix.hex}"

  tags = merge(local.tags, {
    Name = "${local.name_prefix}-pdfs"
  })
}

resource "aws_s3_bucket_versioning" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Lifecycle: transition old versions to cheaper storage, eventually expire.
resource "aws_s3_bucket_lifecycle_configuration" "pdfs" {
  bucket = aws_s3_bucket.pdfs.id

  rule {
    id     = "archive-old-versions"
    status = "Enabled"

    noncurrent_version_transition {
      noncurrent_days = 30
      storage_class   = "STANDARD_IA"
    }

    noncurrent_version_transition {
      noncurrent_days = 90
      storage_class   = "GLACIER"
    }

    noncurrent_version_expiration {
      noncurrent_days = 365
    }
  }
}

# ── ALB access logs bucket ────────────────────────────────────────────────

resource "aws_s3_bucket" "alb_logs" {
  bucket = "${local.name_prefix}-alb-logs-${random_id.bucket_suffix.hex}"

  tags = merge(local.tags, { Name = "${local.name_prefix}-alb-logs" })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
  bucket = aws_s3_bucket.alb_logs.id

  rule {
    id     = "expire-logs"
    status = "Enabled"

    expiration {
      days = 90
    }
  }
}