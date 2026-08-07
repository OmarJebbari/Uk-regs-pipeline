# =============================================================================
# Route 53 — hosted zone + DNS validation records + ALB alias
# =============================================================================

# ── Hosted zone (only created when create_route53_zone = true) ────────────

resource "aws_route53_zone" "main" {
  count = var.create_route53_zone ? 1 : 0

  name = var.route53_zone_name

  tags = merge(local.tags, { Name = "${local.name_prefix}-zone" })
}

# ── Apex A-record (alias) → ALB ───────────────────────────────────────────
# This points api.ukregs.example.com at the ALB's DNS name.

resource "aws_route53_record" "api" {
  count = var.create_route53_zone ? 1 : 0

  zone_id = aws_route53_zone.main[0].zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

# ── Data source: existing zone (when create_route53_zone = false) ──────────
# Lets the ACM validation records still be written if you manage DNS elsewhere.

data "aws_route53_zone" "existing" {
  count        = var.create_route53_zone ? 0 : 1
  name         = var.route53_zone_name
  private_zone = false
}