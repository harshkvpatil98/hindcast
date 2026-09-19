# Cost governance: the AWS-side second line behind the in-process cost meter.
#
# The meter (cascade/llm/meter.py) aborts a phase the moment it crosses its
# ceiling -- but only for spend it can see, from inside the process. A budget
# sees the whole account from outside: a runaway Aurora cluster, a task left
# running, a bug in the meter itself. The two are reconciled at M8's gate.
#
# The ceilings are READ from configs/base.yaml, not restated, so the alarm
# cannot drift from the limit the code enforces.

data "aws_caller_identity" "current" {}

locals {
  study          = yamldecode(file(var.study_config_path))
  phase_ceilings = local.study.budget.phase_ceiling_usd
  study_ceiling  = sum(values(local.phase_ceilings))
  monthly_limit  = local.study_ceiling + var.infrastructure_allowance_usd
  account        = data.aws_caller_identity.current.account_id
}

# --- Alerts topic -----------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name              = "${var.name}-cost-alerts"
  kms_master_key_id = var.kms_key_arn
}

data "aws_iam_policy_document" "alerts" {
  source_policy_documents = var.extra_topic_policy_documents

  statement {
    sid       = "CostServicesPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com", "costalerts.amazonaws.com"]
    }
    # Only on behalf of this account: without it, any account's budget could
    # publish here (the confused-deputy problem).
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts.json
}

resource "aws_sns_topic_subscription" "email" {
  for_each  = var.alert_emails
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# --- Budget -----------------------------------------------------------------------
#
# One budget over the whole account, which is why the study runs in a dedicated
# account (ADR-0035): model spend through Claude Platform on AWS is billed via
# AWS Marketplace, where cost-allocation tags do not reach, so a tag-filtered
# budget would silently exclude the largest line item.

resource "aws_budgets_budget" "study" {
  name         = "${var.name}-study"
  budget_type  = "COST"
  limit_amount = format("%.2f", local.monthly_limit)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator       = "GREATER_THAN"
      threshold                 = notification.value
      threshold_type            = "PERCENTAGE"
      notification_type         = "ACTUAL"
      subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
    }
  }

  # Forecasted: fires while there is still time to stop, not after.
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.alerts.arn]
  }
}

# --- Anomaly detection ---------------------------------------------------------------

resource "aws_ce_anomaly_monitor" "services" {
  name              = "${var.name}-services"
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

resource "aws_ce_anomaly_subscription" "alerts" {
  name             = "${var.name}-anomalies"
  frequency        = "IMMEDIATE"
  monitor_arn_list = [aws_ce_anomaly_monitor.services.arn]

  subscriber {
    type    = "SNS"
    address = aws_sns_topic.alerts.arn
  }

  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      match_options = ["GREATER_THAN_OR_EQUAL"]
      values        = [tostring(var.anomaly_threshold_usd)]
    }
  }

  depends_on = [aws_sns_topic_policy.alerts]
}
