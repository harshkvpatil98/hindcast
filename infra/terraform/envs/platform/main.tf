# M12: the platform around the study -- cost governance, the event lake, and
# the organization's guardrails. Designed and gated offline; not applied
# (ADR-0035).

variable "region" {
  description = "AWS region. Required, never defaulted (ADR-0028)."
  type        = string
}

variable "name" {
  type    = string
  default = "cascade"
}

variable "infrastructure_allowance_usd" {
  description = "Monthly allowance for non-model spend. Required: see modules/governance."
  type        = number
}

variable "alert_emails" {
  type    = set(string)
  default = []
}

variable "allowed_regions" {
  description = "Regions workload accounts may use. Defaults to the platform's own."
  type        = list(string)
  default     = []
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

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account = data.aws_caller_identity.current.account_id
}

data "aws_iam_policy_document" "key" {
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
}

module "eventlake" {
  source        = "../../modules/eventlake"
  name          = var.name
  bucket_suffix = "${local.account}-${var.region}"
  kms_key_arn   = aws_kms_key.platform.arn
}

module "guardrails" {
  source          = "../../modules/guardrails"
  name            = var.name
  allowed_regions = length(var.allowed_regions) > 0 ? var.allowed_regions : [var.region]
  target_ids      = var.guardrail_target_ids
}

output "study_ceiling_usd" {
  value = module.governance.study_ceiling_usd
}

output "monthly_limit_usd" {
  value = module.governance.monthly_limit_usd
}

output "events_bucket" {
  value = module.eventlake.events_bucket
}
