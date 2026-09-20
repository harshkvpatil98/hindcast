# Two kinds of guardrail, at two layers, answering one question: what this
# account may not do.
#
#   1. Service control policies -- what no principal may ask the AWS API for,
#      whatever its IAM policies say. Preventive, from the management account.
#   2. A Bedrock guardrail -- what a model may not emit. Created here and
#      applied nowhere (ADR-0050); see the second half of this file.
#
# Each SCP statement removes a way this project's own guarantees could be undone
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

# --- The Bedrock guardrail (ADR-0050) -------------------------------------------------------------
#
# Created so it can be *measured*, not so it can filter. `cascade eval
# guardrails` reads the 180 compiled graphs and asks this guardrail what it
# would have done to each; a guardrail that would alter even one decomposition
# is reported as a confound, because the arm that ran with it and the arm that
# ran without it would no longer be the same experiment. ADR-0030 deferred
# guardrails partly for that reason and named this audit as what would settle
# it. Nothing in the compile path reads these outputs.
#
# Null by default, exactly as `target_ids` is empty by default: a control that
# is created and bound to nothing can be reviewed before it can affect anyone.

resource "aws_bedrock_guardrail" "model" {
  count = var.model_guardrail == null ? 0 : 1

  name        = var.model_guardrail.name
  description = var.model_guardrail.description

  # Required by the service. The audit reads `action`, never the substitute
  # text, so these are inert here -- and they are still named rather than
  # defaulted, because a later deployment that puts this guardrail in a call
  # path would return them to a caller.
  blocked_input_messaging   = var.model_guardrail.blocked_input_messaging
  blocked_outputs_messaging = var.model_guardrail.blocked_outputs_messaging

  dynamic "content_policy_config" {
    for_each = length(var.model_guardrail.content_filters) > 0 ? [1] : []
    content {
      dynamic "filters_config" {
        for_each = var.model_guardrail.content_filters
        content {
          type            = filters_config.value.type
          input_strength  = filters_config.value.input_strength
          output_strength = filters_config.value.output_strength
        }
      }
    }
  }

  dynamic "topic_policy_config" {
    for_each = length(var.model_guardrail.denied_topics) > 0 ? [1] : []
    content {
      dynamic "topics_config" {
        for_each = var.model_guardrail.denied_topics
        content {
          name       = topics_config.value.name
          definition = topics_config.value.definition
          examples   = topics_config.value.examples
          # DENY is the only topic type the service defines, and it is what a
          # topic policy is for; spelling it here rather than taking it as an
          # input keeps a caller from configuring a topic that allows.
          type = "DENY"
        }
      }
    }
  }
}

# DRAFT is mutable. An audit quoted against DRAFT measures whatever the
# guardrail happened to be that afternoon and can never be re-checked against
# the same object, which makes the result unfalsifiable -- the property this
# project spends most of its effort avoiding. `skip_destroy` keeps published
# versions: a number quoted in a report must still resolve later, and the
# alternative is a citation to an object that no longer exists.
resource "aws_bedrock_guardrail_version" "model" {
  count = var.model_guardrail != null && var.publish_guardrail_version ? 1 : 0

  guardrail_arn = aws_bedrock_guardrail.model[0].guardrail_arn
  description   = "Immutable version for `cascade eval guardrails` (ADR-0050)"
  skip_destroy  = true
}
