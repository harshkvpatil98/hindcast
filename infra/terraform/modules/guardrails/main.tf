# Service control policies: what no principal in a workload account may do,
# whatever its IAM policies say. Preventive controls, applied from the
# organization's management account.
#
# Each statement removes a way this project's own guarantees could be undone
# from inside the account: the audit trail switched off, a database created
# unencrypted, the account's public-access block lifted, work moved to a region
# nobody chose.

locals {
  # Global services answer from us-east-1 whatever region was requested; a
  # region deny that caught them would break IAM, billing and support.
  global_service_actions = [
    "a4b:*", "account:*", "budgets:*", "ce:*", "cloudfront:*", "cur:*",
    "globalaccelerator:*", "health:*", "iam:*", "organizations:*",
    "route53:*", "route53domains:*", "sts:*", "support:*", "trustedadvisor:*",
    "waf:*",
  ]
}

data "aws_iam_policy_document" "regions" {
  statement {
    sid         = "DenyOutsideAllowedRegions"
    effect      = "Deny"
    not_actions = local.global_service_actions
    resources   = ["*"]
    condition {
      test     = "StringNotEquals"
      variable = "aws:RequestedRegion"
      values   = var.allowed_regions
    }
  }
}

data "aws_iam_policy_document" "integrity" {
  statement {
    sid    = "ProtectTheAuditTrail"
    effect = "Deny"
    actions = [
      "cloudtrail:DeleteTrail",
      "cloudtrail:StopLogging",
      "cloudtrail:UpdateTrail",
      "config:DeleteConfigurationRecorder",
      "config:StopConfigurationRecorder",
      "ec2:DeleteFlowLogs",
      "guardduty:DeleteDetector",
      "guardduty:DisassociateFromMasterAccount",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "StayInTheOrganization"
    effect    = "Deny"
    actions   = ["organizations:LeaveOrganization"]
    resources = ["*"]
  }

  statement {
    sid       = "NoRootUser"
    effect    = "Deny"
    actions   = ["*"]
    resources = ["*"]
    condition {
      test     = "StringLike"
      variable = "aws:PrincipalArn"
      values   = ["arn:aws:iam::*:root"]
    }
  }

  statement {
    sid       = "KeepTheAccountPublicAccessBlock"
    effect    = "Deny"
    actions   = ["s3:PutAccountPublicAccessBlock"]
    resources = ["*"]
  }

  # The evidence corpus and the event log are the study. Neither may exist
  # unencrypted, whoever creates the cluster and however.
  statement {
    sid       = "DatabasesAreEncrypted"
    effect    = "Deny"
    actions   = ["rds:CreateDBCluster", "rds:CreateDBInstance"]
    resources = ["*"]
    condition {
      test     = "Bool"
      variable = "rds:StorageEncrypted"
      values   = ["false"]
    }
  }
}

resource "aws_organizations_policy" "regions" {
  name        = "${var.name}-allowed-regions"
  description = "Deny every regional action outside the allowed regions"
  type        = "SERVICE_CONTROL_POLICY"
  content     = data.aws_iam_policy_document.regions.json
}

resource "aws_organizations_policy" "integrity" {
  name        = "${var.name}-integrity"
  description = "Protect the audit trail, encryption and the public-access block"
  type        = "SERVICE_CONTROL_POLICY"
  content     = data.aws_iam_policy_document.integrity.json
}

resource "aws_organizations_policy_attachment" "regions" {
  for_each  = var.target_ids
  policy_id = aws_organizations_policy.regions.id
  target_id = each.value
}

resource "aws_organizations_policy_attachment" "integrity" {
  for_each  = var.target_ids
  policy_id = aws_organizations_policy.integrity.id
  target_id = each.value
}
