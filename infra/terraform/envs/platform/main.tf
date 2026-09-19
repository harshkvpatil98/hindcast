# M12: the platform around the study -- cost governance, the event lake, and
# the organization's guardrails. Designed and gated offline; not applied
# (ADR-0035).

variable "region" {
  description = "AWS region. Required, never defaulted (ADR-0028)."
  type        = string
}

variable "replica_region" {
  description = "Second region for the tier-0 recovery replica. Required, never defaulted."
  type        = string
}

variable "simulation_principal_arns" {
  description = "IAM principals the simulation runs as; denied the labels archive."
  type        = list(string)
  default     = []
}

variable "name" {
  type    = string
  default = "cascade"
}

variable "sandbox_name" {
  description = <<-EOT
    Name prefix of the sandbox whose alarms and task-failure rules alert here.
    The sandbox is created and destroyed; this root must not read its state, so
    publish rights are granted on the ARNs its names make deterministic.
  EOT
  type        = string
  default     = "cascade-sandbox"
}

variable "infrastructure_allowance_usd" {
  description = "Monthly allowance for non-model spend. Required: see modules/governance."
  type        = number
}

variable "alert_emails" {
  type    = set(string)
  default = []
}

variable "additional_allowed_regions" {
  description = <<-EOT
    Regions workload accounts may use besides this root's own two (primary and
    replica), which are always allowed -- the region SCP would otherwise deny
    the platform's own replica bucket.
  EOT
  type        = list(string)
  default     = []
}

variable "guardduty_min_severity" {
  description = "Lowest GuardDuty severity published to the alerts topic. Required: see modules/audit."
  type        = number
}

variable "guardrail_target_ids" {
  description = "OUs or accounts the SCPs attach to. Empty = created, bound to nothing."
  type        = set(string)
  default     = []
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "cascade", Environment = var.name, Milestone = "M12", ManagedBy = "terraform" }
  }
}

provider "aws" {
  alias  = "replica"
  region = var.replica_region
  default_tags {
    tags = { Project = "cascade", Environment = var.name, Milestone = "M12", ManagedBy = "terraform" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
}

data "aws_iam_policy_document" "key" {
  # CloudTrail and CloudWatch Logs must be able to use this key for the audit
  # trail. The statements come from the module that knows what it needs, so the
  # key policy cannot fall out of step with the trail it serves.
  source_policy_documents = [module.audit.required_key_policy_statements_json]

  #checkov:skip=CKV_AWS_111:A KMS key policy's "kms:*" for the account root is AWS's default key policy: it delegates to IAM, and Resource "*" in a key policy means this key only.
  #checkov:skip=CKV_AWS_356:Resource "*" in a key policy refers to the key itself, not to all resources.
  #checkov:skip=CKV_AWS_109:As above -- the account-root statement is the standard delegation to IAM.
  statement {
    sid       = "AccountAdministers"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${local.account}:root"]
    }
  }

  # CloudWatch alarms publish to the same encrypted topic.
  statement {
    sid       = "AlarmsUseTheAlertsKey"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }

  # EventBridge too -- and this statement carries NO condition, on purpose. The
  # SNS developer guide states that source conditions in a KMS key policy are
  # not supported for EventBridge-to-encrypted-topic delivery: copying the
  # SourceAccount pattern from the statements around it would drop every
  # task-failure alert, with no error anywhere. Not verified live.
  statement {
    sid       = "EventBridgeUsesTheAlertsKey"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }

  # Budgets and Cost Anomaly Detection publish to an encrypted topic, so they
  # must be able to use its key.
  statement {
    sid       = "CostServicesUseTheAlertsKey"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com", "costalerts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

# Who, beyond the cost services, may publish to the alerts topic.
data "aws_iam_policy_document" "sandbox_alerts" {
  statement {
    sid       = "SandboxTaskFailureRulesPublish"
    actions   = ["sns:Publish"]
    resources = ["arn:${data.aws_partition.current.partition}:sns:${var.region}:${local.account}:${var.name}-cost-alerts"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:events:${var.region}:${local.account}:rule/${var.sandbox_name}-task-*"]
    }
  }
  statement {
    sid       = "SandboxAlarmsPublish"
    actions   = ["sns:Publish"]
    resources = ["arn:${data.aws_partition.current.partition}:sns:${var.region}:${local.account}:${var.name}-cost-alerts"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:cloudwatch:${var.region}:${local.account}:alarm:${var.sandbox_name}-*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_kms_key" "platform" {
  description             = "${var.name}: event lake, query results, cost alerts"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.key.json
}

module "governance" {
  source                       = "../../modules/governance"
  name                         = var.name
  study_config_path            = "${path.module}/../../../../configs/base.yaml"
  infrastructure_allowance_usd = var.infrastructure_allowance_usd
  kms_key_arn                  = aws_kms_key.platform.arn
  alert_emails                 = var.alert_emails
  # The sandbox's rules and alarms, and the audit module's findings rule: an
  # SNS topic has one policy, and each thing that publishes must be admitted
  # by it -- by ARN -- or is dropped with no error.
  extra_topic_policy_documents = [data.aws_iam_policy_document.sandbox_alerts.json, module.audit.required_topic_policy_statements_json]
}

module "eventlake" {
  source        = "../../modules/eventlake"
  name          = var.name
  bucket_suffix = "${local.account}-${var.region}"
  kms_key_arn   = aws_kms_key.platform.arn
}

module "recovery" {
  source = "../../modules/recovery"
  providers = {
    aws         = aws
    aws.replica = aws.replica
  }
  name                      = var.name
  bucket_suffix             = local.account
  kms_key_arn               = aws_kms_key.platform.arn
  simulation_principal_arns = var.simulation_principal_arns
}

module "audit" {
  source        = "../../modules/audit"
  name          = var.name
  bucket_suffix = "${local.account}-${var.region}"
  kms_key_arn   = aws_kms_key.platform.arn
  # Findings above the threshold go where the budget alerts go: one topic, one
  # set of subscribers, one place to look.
  alerts_topic_arn       = module.governance.alerts_topic_arn
  guardduty_min_severity = var.guardduty_min_severity
  # The lake and the recovery bucket deliberately have no S3 access logging:
  # these data events, written to a locked trail, are their access record.
  data_event_bucket_arns = [
    "arn:${data.aws_partition.current.partition}:s3:::${module.eventlake.events_bucket}",
    "arn:${data.aws_partition.current.partition}:s3:::${module.recovery.bucket}",
  ]
}

module "guardrails" {
  source          = "../../modules/guardrails"
  name            = var.name
  allowed_regions = distinct(concat([var.region, var.replica_region], var.additional_allowed_regions))
  target_ids      = var.guardrail_target_ids
}

output "alerts_topic_arn" {
  description = "Feed this to the sandbox's `observability.alerts_topic_arn`."
  value       = module.governance.alerts_topic_arn
}

output "study_ceiling_usd" {
  value = module.governance.study_ceiling_usd
}

output "monthly_limit_usd" {
  value = module.governance.monthly_limit_usd
}

output "recovery_registry_prefix" {
  value = module.recovery.registry_prefix
}

output "recovery_source_cache_prefix" {
  description = "Upload ledger.source_cache_dir here after every `ledger seal` (dr-runbook.md), with `recovery_archive_write_policy_arn`."
  value       = module.recovery.source_cache_prefix
}

output "recovery_archive_write_policy_arn" {
  description = "Add-only access to the registry and source-cache archives, for whoever uploads them."
  value       = module.recovery.archive_write_policy_arn
}

# Feed this whole object to the sandbox's `study.recovery`: where the LLM cache
# is archived, under which key, and under which prefix -- passed, so the two
# roots cannot spell the prefix differently.
output "recovery" {
  value = {
    bucket_arn       = module.recovery.bucket_arn
    kms_key_arn      = aws_kms_key.platform.arn
    llm_cache_prefix = module.recovery.prefixes.llm_cache
  }
}

output "events_bucket" {
  value = module.eventlake.events_bucket
}
