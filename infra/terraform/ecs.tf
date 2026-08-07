# =============================================================================
# ECS Fargate — cluster, ALB, API service, worker task definition
# =============================================================================

# ── Cluster ───────────────────────────────────────────────────────────────

resource "aws_ecs_cluster" "main" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(local.tags, { Name = "${local.name_prefix}-cluster" })
}

# ── CloudWatch log group ──────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name_prefix}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.app.arn

  tags = merge(local.tags, { Name = "${local.name_prefix}-logs" })
}

# ── Shared container definitions locals ───────────────────────────────────

locals {
  container_env = [
    { name = "UK_REGS_BACKEND", value = "postgres" },
    { name = "LOG_FORMAT", value = "json" },
    { name = "UK_REGS_S3_BUCKET", value = aws_s3_bucket.pdfs.id },
    { name = "UK_REGS_S3_PREFIX", value = var.s3_pdf_prefix },
    { name = "AWS_REGION", value = var.aws_region },
  ]

  container_secrets = [
    {
      name      = "DATABASE_URL"
      valueFrom = "${aws_secretsmanager_secret.db_creds.arn}:password::"
      # NOTE: the app reads POSTGRES_* and assembles the URL, but we also expose
      # the full secret JSON so the wrapper script can build DATABASE_URL.
    },
    {
      name      = "OPENAI_API_KEY"
      valueFrom = "${aws_secretsmanager_secret.openai.arn}:api_key::"
    },
    {
 name      = "API_KEY"
      valueFrom = "${aws_secretsmanager_secret.app_api_key.arn}:api_key::"
    },
  ]

  api_image = "${aws_ecr_repository.main["api"].repository_url}:latest"
}

# ── API task definition ───────────────────────────────────────────────────

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.api_image
      essential = true

      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]

      environment = local.container_env
      secrets     = local.container_secrets

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f ${var.api_health_check_path} || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "api"
        }
      }
    }
  ])

  tags = merge(local.tags, { Name = "${local.name_prefix}-api-task" })
}

# ── ALB ───────────────────────────────────────────────────────────────────

resource "aws_lb" "main" {
  name               = "${local.name_prefix}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  drop_invalid_header_fields = true

  access_logs {
    bucket  = aws_s3_bucket.alb_logs.id
    prefix  = "alb"
    enabled = true
  }

  tags = merge(local.tags, { Name = "${local.name_prefix}-alb" })
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name_prefix}-api-tg"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = var.api_health_check_path
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200"
  }

  tags = merge(local.tags, { Name = "${local.name_prefix}-api-tg" })
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = "443"
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.main.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# ── ECS API service ───────────────────────────────────────────────────────

resource "aws_ecs_service" "api" {
  name            = "${local.name_prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  health_check_grace_period_seconds = 90
  enable_execute_command            = false

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # Force new deployments only when the task def changes, not on every apply.
  triggers = {
    redeployment = timestamp()
  }

  tags = merge(local.tags, { Name = "${local.name_prefix}-api-service" })

  depends_on = [aws_lb_listener.https]
}

# ── Worker task definition (one-shot, run via RunTask) ────────────────────

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024 # 1 vCPU — PDF parsing is CPU-bound
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = "${aws_ecr_repository.main["worker"].repository_url}:latest"
      essential = true

      environment = concat(local.container_env, [
        { name = "UK_REGS_WORKER_MODE", value = "ingest" }
      ])
      secrets = local.container_secrets

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          awslogs-group         = aws_cloudwatch_log_group.app.name
          awslogs-region        = var.aws_region
          awslogs-stream-prefix = "worker"
        }
      }
    }
  ])

  tags = merge(local.tags, { Name = "${local.name_prefix}-worker-task" })
}

# ── ACM certificate (DNS-validated) ───────────────────────────────────────

resource "aws_acm_certificate" "main" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }

  tags = merge(local.tags, { Name = "${local.name_prefix}-cert" })
}

resource "aws_route53_record" "cert_validation" {
  count = var.create_route53_zone ? 1 : 0

  zone_id = aws_route53_zone.main[0].zone_id
  name    = tolist(aws_acm_certificate.main.domain_validation_options)[0].resource_record_name
  type    = tolist(aws_acm_certificate.main.domain_validation_options)[0].resource_record_type
  records = [tolist(aws_acm_certificate.main.domain_validation_options)[0].resource_record_value]
  ttl     = 60
}

resource "aws_acm_certificate_validation" "main" {
  count                   = var.create_route53_zone ? 1 : 0
  certificate_arn         = aws_acm_certificate.main.arn
  validation_record_fqdns = [aws_route53_record.cert_validation[0].fqdn]
}