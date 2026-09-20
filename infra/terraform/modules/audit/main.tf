# The account's audit trail: who did what (CloudTrail), what changed (Config),
# and what looks like an attack (GuardDuty).
#
# modules/guardrails already denies stopping all three. Until this module
# existed that SCP protected nothing: a guardrail around an audit trail nobody
# had created. This is the trail.
#
# It is also the access record for the event lake and the recovery buckets.
# Both skip S3 access logging on purpose -- a logging bucket per bucket
# multiplies what must itself be protected -- and name CloudTrail data events
# as the replacement. `data_event_bucket_arns` is where that promise is kept.
#
# THE KEY IS THE CALLER'S, AND SO IS ITS POLICY. The root's key policy must
# carry three statements, or CreateTrail and CreateLogGroup are refused:
#
#   1. cloudtrail.amazonaws.com : kms:GenerateDataKey*
#        StringEquals aws:SourceArn                            = <this trail's ARN>
#        StringLike   kms:EncryptionContext:aws:cloudtrail:arn = arn:aws:cloudtrail:*:<account>:trail/*
#   2. cloudtrail.amazonaws.com : kms:DescribeKey
#        StringEquals aws:SourceArn                            = <this trail's ARN>
#   3. logs.<region>.amazonaws.com : kms:Encrypt*, kms:Decrypt*, kms:ReEncrypt*,
#                                    kms:GenerateDataKey*, kms:Describe*
#        ArnEquals    kms:EncryptionContext:aws:logs:arn       = <this log group's ARN>
#
# They are written once, below, and exported as
# `required_key_policy_statements_json`, so the root merges them with
# `source_policy_documents` instead of restating names that would drift. The
# document reads only the module's name, the account and the region -- never
# the key -- so feeding it back into the key's own policy is not a cycle.
# AWS Config needs no statement: it reaches the key through its role, and the
# account-root statement already delegates that to IAM.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  region    = data.aws_region.current.region

  trail_name     = "${var.name}-audit"
  log_group_name = "/cascade/${var.name}/cloudtrail"
  trail_bucket   = "${var.name}-audit-trail-${var.bucket_suffix}"
  config_bucket  = "${var.name}-audit-config-${var.bucket_suffix}"

  # Built from names, not read from the resources. The bucket policy has to
  # name the trail before the trail exists -- CloudTrail checks the policy when
  # the trail is created -- and the key-policy statements must not depend on
  # anything that depends on the key.
  trail_arn         = "arn:${local.partition}:cloudtrail:${local.region}:${local.account}:trail/${local.trail_name}"
  log_group_arn     = "arn:${local.partition}:logs:${local.region}:${local.account}:log-group:${local.log_group_name}"
  trail_bucket_arn  = "arn:${local.partition}:s3:::${local.trail_bucket}"
  config_bucket_arn = "arn:${local.partition}:s3:::${local.config_bucket}"
}

# --- The trail's bucket: locked ------------------------------------------------------

resource "aws_s3_bucket" "trail" {
  #checkov:skip=CKV_AWS_18:The regress stops here. Access logs for the log bucket need a second log bucket, and data events on it would loop; what protects these objects is the lock, whoever reads them.
  #checkov:skip=CKV_AWS_144:The DR runbook replicates tier 0 only; replicating the audit record is a decision for docs/architecture/dr-runbook.md, not a default.
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events from the trail's bucket; the searchable copy is the log group.
  #checkov:skip=CKV2_AWS_61:No lifecycle rule on purpose: objects here are under Object Lock, and an expiry rule is a standing attempt to delete them.
  bucket              = local.trail_bucket
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "trail" {
  bucket = aws_s3_bucket.trail.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Log-file validation proves a log was altered; it cannot stop the alteration,
# or a deletion. The lock does: no version of a log or digest file can be
# overwritten or removed until its retention ends. GOVERNANCE, because a
# sandbox account must be able to be torn down by someone explicitly permitted
# to; lifting it is itself a call this trail records.
resource "aws_s3_bucket_object_lock_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.trail]
}

resource "aws_s3_bucket_ownership_controls" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "trail" {
  bucket                  = aws_s3_bucket.trail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "trail" {
  bucket = aws_s3_bucket.trail.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    # Off, unlike every other bucket here. With a bucket key CloudTrail also
    # needs kms:Decrypt, which AWS documents with no condition at all -- on a
    # key that also encrypts the event lake. A bucket key saves KMS calls, and
    # a trail writes a few files every five minutes: there is nothing to save.
    bucket_key_enabled = false
  }
}

data "aws_iam_policy_document" "trail_bucket" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [local.trail_bucket_arn, "${local.trail_bucket_arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # cloudtrail.amazonaws.com is one principal for every trail in every
  # account. Without aws:SourceArn, anybody's trail could be pointed at this
  # bucket and write into the record (the confused-deputy problem); with it,
  # only this trail can.
  statement {
    sid       = "CloudTrailChecksTheAcl"
    actions   = ["s3:GetBucketAcl"]
    resources = [local.trail_bucket_arn]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }

  statement {
    sid       = "CloudTrailDelivers"
    actions   = ["s3:PutObject"]
    resources = ["${local.trail_bucket_arn}/AWSLogs/${local.account}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
    # CloudTrail sends this ACL on every delivery, and S3 accepts it on a
    # bucket with ACLs disabled. Kept because it is the policy AWS documents.
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
}

resource "aws_s3_bucket_policy" "trail" {
  bucket = aws_s3_bucket.trail.id
  policy = data.aws_iam_policy_document.trail_bucket.json

  # S3 refuses a policy written while the public-access block is still being
  # applied (OperationAborted), and whether it happens is a race.
  depends_on = [aws_s3_bucket_public_access_block.trail]
}

# --- The searchable copy: CloudWatch Logs ----------------------------------------------

resource "aws_cloudwatch_log_group" "trail" {
  name              = local.log_group_name
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

data "aws_iam_policy_document" "trail_logs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "trail_logs" {
  name               = "${var.name}-audit-trail-logs"
  description        = "CloudTrail: write this trail's events to its log group, nothing else"
  assume_role_policy = data.aws_iam_policy_document.trail_logs_assume.json
}

data "aws_iam_policy_document" "trail_logs" {
  statement {
    sid     = "WriteThisTrailsStreams"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    # CloudTrail names its streams <account>_CloudTrail_<home region>; the
    # role can write those and no other stream in the group.
    resources = ["${local.log_group_arn}:log-stream:${local.account}_CloudTrail_${local.region}*"]
  }
}

resource "aws_iam_role_policy" "trail_logs" {
  name   = "write-trail-events"
  role   = aws_iam_role.trail_logs.id
  policy = data.aws_iam_policy_document.trail_logs.json
}

# --- The trail ---------------------------------------------------------------------------

resource "aws_cloudtrail" "this" {
  #checkov:skip=CKV_AWS_252:An SNS notice per delivered log file has no consumer here; alerting reads the log group, and a topic nobody subscribes to is one more thing to secure.
  name           = local.trail_name
  s3_bucket_name = aws_s3_bucket.trail.bucket
  kms_key_id     = var.kms_key_arn

  # Every region, including the ones guardrails denies: a region nobody chose
  # is exactly where activity would be worth seeing. And global-service
  # events, or IAM and STS -- who became whom -- are missing from the record.
  is_multi_region_trail         = true
  include_global_service_events = true
  enable_logging                = true

  # Hourly digest files, signed and chained. Without them a log file that was
  # edited, or removed, reads the same as one that never existed.
  enable_log_file_validation = true

  cloud_watch_logs_group_arn = "${aws_cloudwatch_log_group.trail.arn}:*"
  cloud_watch_logs_role_arn  = aws_iam_role.trail_logs.arn

  advanced_event_selector {
    name = "Management events, reads and writes"
    field_selector {
      field  = "eventCategory"
      equals = ["Management"]
    }
  }

  # The caller's buckets, and always this module's Config bucket: it has no
  # lock, so the record of who deleted from it has to live somewhere that does.
  advanced_event_selector {
    name = "S3 object reads and writes in the named buckets"
    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }
    field_selector {
      field  = "resources.type"
      equals = ["AWS::S3::Object"]
    }
    # A prefix match on the object ARN. The trailing slash is what keeps
    # "cascade-events" from also selecting "cascade-events-archive".
    field_selector {
      field       = "resources.ARN"
      starts_with = [for arn in distinct(concat(var.data_event_bucket_arns, [local.config_bucket_arn])) : "${arn}/"]
    }
  }

  lifecycle {
    # Each delivery to the log bucket would be a data event, logged by a
    # delivery, which is a data event: a loop billed per event.
    precondition {
      condition     = !contains(var.data_event_bucket_arns, local.trail_bucket_arn)
      error_message = "data_event_bucket_arns must not name the trail's own log bucket: every delivery would generate the next one."
    }
  }

  # CloudTrail tests both destinations when the trail is created, so the
  # permissions must exist first; neither is referenced above.
  depends_on = [aws_s3_bucket_policy.trail, aws_iam_role_policy.trail_logs]
}

# --- GuardDuty -----------------------------------------------------------------------------

resource "aws_guardduty_detector" "this" {
  #checkov:skip=CKV2_AWS_3:Organization-wide auto-enable is set from a delegated administrator, and there is no organization yet (ADR-0035): this detector covers the one account that exists.
  enable = true
  # How soon a finding's UPDATES reach EventBridge (a new finding is sent
  # within minutes regardless). Fifteen is the shortest offered and costs
  # nothing: a recurring finding is a finding still happening.
  finding_publishing_frequency = "FIFTEEN_MINUTES"
}

# --- Findings go somewhere (ADR-0042) -------------------------------------------------------------
#
# A detector whose findings nobody reads is the audit-trail version of a
# budget with no alarm. Findings at or above a severity the caller chooses go
# to the alerts topic; below it they stay in the console. The threshold has NO
# default: "what severity pages a human" is a decision about this account,
# and a default here would be somebody else's.
#
# GuardDuty's scale: 1.0-3.9 Low, 4.0-6.9 Medium, 7.0-8.9 High, 9.0-10.0
# Critical.

locals {
  findings_rule_name = "${var.name}-guardduty-findings"
  # Built from the name, like the trail ARN above: the topic policy statement
  # this module hands the caller must exist before the rule does.
  findings_rule_arn = "arn:${local.partition}:events:${local.region}:${local.account}:rule/${local.findings_rule_name}"

  delivery_alarm_name = "${var.name}-guardduty-alert-delivery"
  delivery_alarm_arn  = "arn:${local.partition}:cloudwatch:${local.region}:${local.account}:alarm:${local.delivery_alarm_name}"
}

resource "aws_cloudwatch_event_rule" "findings" {
  name        = local.findings_rule_name
  description = "GuardDuty findings of severity ${var.guardduty_min_severity} or higher, to the alerts topic"

  event_pattern = jsonencode({
    source        = ["aws.guardduty"]
    "detail-type" = ["GuardDuty Finding"]
    detail = {
      severity = [{ numeric = [">=", var.guardduty_min_severity] }]
    }
  })
}

resource "aws_cloudwatch_event_target" "findings" {
  rule      = aws_cloudwatch_event_rule.findings.name
  target_id = "alerts"
  arn       = var.alerts_topic_arn

  # The finding, in the order someone deciding what to do needs it. The raw
  # event is several KB of JSON; the console has that.
  input_transformer {
    input_paths = {
      severity    = "$.detail.severity"
      type        = "$.detail.type"
      title       = "$.detail.title"
      description = "$.detail.description"
      resource    = "$.detail.resource.resourceType"
      region      = "$.region"
      account     = "$.account"
      id          = "$.detail.id"
      count       = "$.detail.service.count"
    }
    input_template = join("\n", [for line in [
      "GuardDuty finding, severity <severity>: <type>",
      "<title>",
      "<description>",
      "resource type: <resource>; account <account>, region <region>; seen <count> time(s)",
      "finding id <id> -- open it in the GuardDuty console for the full record",
    ] : "\"${line}\""])
  }
}

# A rule that matched and could not deliver is silence with a green light. The
# topic policy and its KMS statement are the two ways delivery fails, and
# neither failure is reported anywhere else.
resource "aws_cloudwatch_metric_alarm" "findings_delivery" {
  alarm_name          = local.delivery_alarm_name
  alarm_description   = "EventBridge matched a GuardDuty finding and could not publish it to the alerts topic (rule ${local.findings_rule_name}). Check the topic policy and the key policy."
  namespace           = "AWS/Events"
  metric_name         = "FailedInvocations"
  dimensions          = { RuleName = aws_cloudwatch_event_rule.findings.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
}

# --- AWS Config: what changed, not only who called ---------------------------------------
#
# Config's history does NOT go to the locked bucket. AWS Config "does not
# support the delivery channel to an Amazon S3 bucket where object lock is
# enabled with default retention enabled" (its developer guide, Working with
# the Delivery Channel) -- a refusal that arrives at apply, which no offline
# gate reaches. So the history gets a bucket of its own:
# versioned, with permanent deletion denied to everyone in the bucket policy.
# Removing a version then takes two acts, editing that policy and deleting,
# and the locked trail records both -- the second because this bucket is
# always among the trail's data events.

resource "aws_s3_bucket" "config" {
  #checkov:skip=CKV_AWS_18:Access logging is this module's own trail: the bucket is always among its S3 data events, as for the event lake and the recovery buckets.
  #checkov:skip=CKV_AWS_144:The DR runbook replicates tier 0 only; replicating the audit record is a decision for docs/architecture/dr-runbook.md, not a default.
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events from the Config history bucket.
  #checkov:skip=CKV2_AWS_61:No lifecycle rule on purpose: expiry is carried out by S3 itself, so it is the one deleter the bucket policy's deny would not bind.
  bucket = local.config_bucket
}

resource "aws_s3_bucket_versioning" "config" {
  bucket = aws_s3_bucket.config.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_ownership_controls" "config" {
  bucket = aws_s3_bucket.config.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket                  = aws_s3_bucket.config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "config" {
  bucket = aws_s3_bucket.config.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "config_bucket" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [local.config_bucket_arn, "${local.config_bucket_arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  # What the lock would have done. An ordinary delete leaves a marker and the
  # version behind it; this is the call that removes the version itself.
  statement {
    sid       = "HistoryIsNeverPermanentlyDeleted"
    effect    = "Deny"
    actions   = ["s3:DeleteObjectVersion"]
    resources = ["${local.config_bucket_arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_s3_bucket_policy" "config" {
  bucket     = aws_s3_bucket.config.id
  policy     = data.aws_iam_policy_document.config_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.config]
}

data "aws_iam_policy_document" "config_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["config.amazonaws.com"]
    }
    # Only on behalf of this account's recorder.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "config" {
  name               = "${var.name}-audit-config"
  description        = "AWS Config: read resource configurations, deliver history to its bucket"
  assume_role_policy = data.aws_iam_policy_document.config_assume.json
}

resource "aws_iam_role_policy_attachment" "config" {
  role       = aws_iam_role.config.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AWS_ConfigRole"
}

# AWS_ConfigRole reads configurations; it cannot write to a bucket. Within one
# account Config delivers as this role, so the role is where delivery is
# granted -- and the bucket policy needs no service principal at all.
data "aws_iam_policy_document" "config_delivery" {
  statement {
    sid       = "FindTheBucket"
    actions   = ["s3:GetBucketAcl", "s3:ListBucket"]
    resources = [local.config_bucket_arn]
  }
  statement {
    sid       = "DeliverHistory"
    actions   = ["s3:PutObject"]
    resources = ["${local.config_bucket_arn}/AWSLogs/${local.account}/Config/*"]
  }
  statement {
    sid       = "EncryptIt"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "config_delivery" {
  name   = "deliver-history"
  role   = aws_iam_role.config.id
  policy = data.aws_iam_policy_document.config_delivery.json
}

resource "aws_config_configuration_recorder" "this" {
  name     = var.name
  role_arn = aws_iam_role.config.arn

  # Everything, including types AWS adds later, and the global ones (IAM): a
  # recorder that lists its types is silent about whatever the list forgot.
  recording_group {
    all_supported                 = true
    include_global_resource_types = true
  }
}

resource "aws_config_delivery_channel" "this" {
  name           = var.name
  s3_bucket_name = aws_s3_bucket.config.bucket
  s3_kms_key_arn = var.kms_key_arn

  # Config writes a test object when the channel is created.
  depends_on = [
    aws_config_configuration_recorder.this,
    aws_iam_role_policy.config_delivery,
    aws_s3_bucket_policy.config,
  ]
}

# A recorder is created stopped, and cannot start without a channel.
resource "aws_config_configuration_recorder_status" "this" {
  name       = aws_config_configuration_recorder.this.name
  is_enabled = true
  depends_on = [aws_config_delivery_channel.this]
}

# --- What the caller's topic policy must carry ------------------------------------------------------
#
# The alerts topic is modules/governance's, and an SNS topic has one policy.
# These two statements admit exactly this module's rule and this module's
# alarm -- by ARN, so no other rule in the account can publish here by being
# an EventBridge rule -- and are merged by the root the way the key-policy
# statements below are. Depends on names, never on the topic.
data "aws_iam_policy_document" "required_topic_policy" {
  statement {
    sid       = "GuardDutyFindingsRulePublishes"
    actions   = ["sns:Publish"]
    resources = [var.alerts_topic_arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.findings_rule_arn]
    }
  }

  statement {
    sid       = "GuardDutyDeliveryAlarmPublishes"
    actions   = ["sns:Publish"]
    resources = [var.alerts_topic_arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [local.delivery_alarm_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

# --- What the caller's key policy must carry ------------------------------------------------

data "aws_iam_policy_document" "required_key_policy" {
  statement {
    sid       = "CloudTrailEncryptsTheTrail"
    actions   = ["kms:GenerateDataKey*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
    condition {
      test     = "StringLike"
      variable = "kms:EncryptionContext:aws:cloudtrail:arn"
      values   = ["arn:${local.partition}:cloudtrail:*:${local.account}:trail/*"]
    }
  }

  statement {
    sid       = "CloudTrailDescribesTheKey"
    actions   = ["kms:DescribeKey"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudtrail.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceArn"
      values   = [local.trail_arn]
    }
  }

  # The Logs principal is regional, and the encryption context pins the grant
  # to this one log group rather than to every group in the account.
  statement {
    sid       = "CloudWatchLogsEncryptsTheTrailGroup"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = [local.log_group_arn]
    }
  }
}
