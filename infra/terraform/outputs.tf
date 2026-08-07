# =============================================================================
# Outputs — useful after `terraform output`
# =============================================================================

output "pdf_bucket_name" {
  description = "S3 bucket holding original PDFs (immutable, versioned)"
  value       = aws_s3_bucket.pdfs.id
}

output "rds_endpoint" {
  description = "RDS Postgres endpoint (host:port)"
  value       = aws_db_instance.main.endpoint
}

output "rds_arn" {
  description = "RDS instance ARN"
  value       = aws_db_instance.main.arn
}

output "db_credentials_secret_arn" {
  description = "Secrets Manager ARN for DB credentials JSON"
  value       = aws_secretsmanager_secret.db_creds.arn
}

output "openai_secret_arn" {
  description = "Secrets Manager ARN for the OpenAI API key"
  value       = aws_secretsmanager_secret.openai.arn
}

output "api_key_secret_arn" {
  description = "Secrets Manager ARN for the X-API-Key header value"
  value       = aws_secretsmanager_secret.app_api_key.arn
}

output "ecr_repositories" {
  description = "ECR repository URLs (api / worker / mcp)"
  value = {
    for k, r in aws_ecr_repository.main : k => r.repository_url
  }
}

output "ecs_cluster_name" {
  description = "Name of the ECS Fargate cluster"
  value       = aws_ecs_cluster.main.name
}

output "api_service_name" {
  description = "Name of the API ECS service"
  value       = aws_ecs_service.api.name
}

output "worker_task_definition_arn" {
  description = "Task definition ARN for one-shot worker runs (RunTask)"
  value       = aws_ecs_task_definition.worker.arn
}

output "alb_dns_name" {
  description = "Public DNS name of the ALB"
  value       = aws_lb.main.dns_name
}

output "api_url" {
  description = "Public HTTPS URL of the API (once DNS validates)"
  value       = "https://${var.domain_name}"
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group for all containers"
  value       = aws_cloudwatch_log_group.app.name
}

output "kms_key_arn" {
  description = "KMS key used for secrets, ECR, logs"
  value       = aws_kms_key.app.arn
}