# The audit trail, offline: CloudTrail to a locked bucket, GuardDuty, AWS Config.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_rds_cluster" {
    defaults = {
      arn                = "arn:aws:rds:us-east-1:123456789012:cluster:mock"
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:master", kms_key_id = "k", secret_status = "active" }]
    }
  }
  # Computed ARNs that flow into ARN-typed arguments must look like ARNs: the
  # provider validates them even under mocks, which is worth keeping.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/mock", key_id = "mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/mock", repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mock" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_ce_anomaly_monitor" {
    defaults = { arn = "arn:aws:ce::123456789012:anomalymonitor/mock" }
  }
  mock_resource "aws_athena_workgroup" {
    defaults = { arn = "arn:aws:athena:us-east-1:123456789012:workgroup/mock" }
  }
  mock_resource "aws_organizations_policy" {
    defaults = { id = "p-mock1234" }
  }
  mock_resource "aws_cloudtrail" {
    defaults = { arn = "arn:aws:cloudtrail:us-east-1:123456789012:trail/mock" }
  }
  mock_resource "aws_guardduty_detector" {
    defaults = { arn = "arn:aws:guardduty:us-east-1:123456789012:detector/mock", id = "mock" }
  }
}

# The root module declares aws.replica; a test file that mocks any provider
# replaces them all, so every file in this root must supply both.
mock_provider "aws" {
  alias = "replica"
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_rds_cluster" {
    defaults = {
      arn                = "arn:aws:rds:us-east-1:123456789012:cluster:mock"
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:master", kms_key_id = "k", secret_status = "active" }]
    }
  }
  # Computed ARNs that flow into ARN-typed arguments must look like ARNs: the
  # provider validates them even under mocks, which is worth keeping.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/mock", key_id = "mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/mock", repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mock" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_ce_anomaly_monitor" {
    defaults = { arn = "arn:aws:ce::123456789012:anomalymonitor/mock" }
  }
  mock_resource "aws_athena_workgroup" {
    defaults = { arn = "arn:aws:athena:us-east-1:123456789012:workgroup/mock" }
  }
  mock_resource "aws_organizations_policy" {
    defaults = { id = "p-mock1234" }
  }
  mock_resource "aws_cloudtrail" {
    defaults = { arn = "arn:aws:cloudtrail:us-east-1:123456789012:trail/mock" }
  }
  mock_resource "aws_guardduty_detector" {
    defaults = { arn = "arn:aws:guardduty:us-east-1:123456789012:detector/mock", id = "mock" }
  }
}

variables {
  name                   = "t"
  bucket_suffix          = "123456789012-us-east-1"
  kms_key_arn            = "arn:aws:kms:us-east-1:123456789012:key/mock"
  alerts_topic_arn       = "arn:aws:sns:us-east-1:123456789012:t-cost-alerts"
  guardduty_min_severity = 7
  # Pinned, so a local terraform.tfvars cannot change what is tested.
  create_trail     = true
  enable_guardduty = true
  enable_config    = true
}

run "the_trail_covers_every_region_and_proves_its_own_integrity" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = aws_cloudtrail.this[0].is_multi_region_trail
    error_message = "One region's trail is blind to every other region, including the ones nobody chose."
  }
  assert {
    condition     = aws_cloudtrail.this[0].include_global_service_events
    error_message = "Without global-service events the trail has no IAM and no STS: not who became whom."
  }
  assert {
    condition     = aws_cloudtrail.this[0].enable_log_file_validation
    error_message = "Log-file validation must be on, or an edited log reads the same as an honest one."
  }
  assert {
    condition     = aws_cloudtrail.this[0].enable_logging
    error_message = "A trail that exists and is not logging is the state the SCP forbids reaching."
  }
  assert {
    condition     = aws_cloudtrail.this[0].kms_key_id == var.kms_key_arn
    error_message = "Log files must be encrypted with the CMK."
  }
  assert {
    condition     = aws_cloudtrail.this[0].s3_bucket_name == "t-audit-trail-123456789012-us-east-1"
    error_message = "The trail must deliver to the locked bucket."
  }
}

run "management_events_are_always_recorded_and_no_callers_bucket_is_assumed" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition = length([
      for s in aws_cloudtrail.this[0].advanced_event_selector : s
      if length([for f in s.field_selector : f if f.field == "eventCategory" && f.equals == tolist(["Management"])]) == 1 && length(s.field_selector) == 1
    ]) == 1
    error_message = "Management events must be recorded whole: one selector, with no further filter narrowing it."
  }
  assert {
    condition = flatten([
      for s in aws_cloudtrail.this[0].advanced_event_selector :
      [for f in s.field_selector : f.starts_with if f.field == "resources.ARN"]
    ]) == ["arn:aws:s3:::t-audit-config-123456789012-us-east-1/"]
    error_message = "With no buckets named, the only data events are the module's own Config bucket -- which has no lock, so deletions from it must be on the locked record."
  }
  assert {
    condition = length([
      for s in aws_cloudtrail.this[0].advanced_event_selector : s
      if length([for f in s.field_selector : f if f.field == "readOnly"]) > 0
    ]) == 0
    error_message = "Reads and writes both: a readOnly filter would drop half the record."
  }
}

run "data_events_cover_the_named_buckets_the_config_bucket_and_nothing_else" {
  command = plan
  module {
    source = "../../modules/audit"
  }
  variables {
    data_event_bucket_arns = ["arn:aws:s3:::t-events-x", "arn:aws:s3:::t-recovery-x"]
  }

  assert {
    condition = flatten([
      for s in aws_cloudtrail.this[0].advanced_event_selector :
      [for f in s.field_selector : f.starts_with if f.field == "resources.ARN"]
    ]) == ["arn:aws:s3:::t-events-x/", "arn:aws:s3:::t-recovery-x/", "arn:aws:s3:::t-audit-config-123456789012-us-east-1/"]
    error_message = "Data events must cover the named buckets, the Config bucket, and nothing else; the trailing slash keeps a prefix from selecting a longer-named bucket."
  }
  assert {
    condition = flatten([
      for s in aws_cloudtrail.this[0].advanced_event_selector :
      [for f in s.field_selector : f.equals if f.field == "resources.type"]
    ]) == ["AWS::S3::Object"]
    error_message = "The data events are S3 object events."
  }
  assert {
    condition = sort(flatten([
      for s in aws_cloudtrail.this[0].advanced_event_selector :
      [for f in s.field_selector : f.equals if f.field == "eventCategory"]
    ])) == tolist(["Data", "Management"])
    error_message = "Naming buckets must add data events, not replace the management events."
  }
  assert {
    condition     = output.controls.data_event_prefixes == ["arn:aws:s3:::t-events-x/", "arn:aws:s3:::t-recovery-x/", "arn:aws:s3:::t-audit-config-123456789012-us-east-1/"]
    error_message = "The controls output must report the selectors the trail actually carries."
  }
}

run "the_log_bucket_is_locked_versioned_encrypted_and_private" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = aws_s3_bucket.trail[0].object_lock_enabled
    error_message = "The trail's bucket must have Object Lock: validation detects tampering, only the lock prevents it."
  }
  assert {
    condition     = one(one(aws_s3_bucket_object_lock_configuration.trail[0].rule).default_retention).mode == "GOVERNANCE"
    error_message = "GOVERNANCE by default; COMPLIANCE cannot be undone by anyone."
  }
  assert {
    condition     = one(one(aws_s3_bucket_object_lock_configuration.trail[0].rule).default_retention).days == 365
    error_message = "Every log file must get a default retention."
  }
  assert {
    condition     = one(aws_s3_bucket_versioning.trail[0].versioning_configuration).status == "Enabled"
    error_message = "Object Lock requires versioning."
  }
  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.trail[0].rule).apply_server_side_encryption_by_default).kms_master_key_id == var.kms_key_arn
    error_message = "The log bucket must default to the CMK."
  }
  assert {
    condition     = one(aws_s3_bucket_ownership_controls.trail[0].rule).object_ownership == "BucketOwnerEnforced"
    error_message = "ACLs must be disabled on the log bucket."
  }
  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.trail[0].block_public_acls,
      aws_s3_bucket_public_access_block.trail[0].block_public_policy,
      aws_s3_bucket_public_access_block.trail[0].ignore_public_acls,
      aws_s3_bucket_public_access_block.trail[0].restrict_public_buckets,
    ])
    error_message = "Every public-access block must be on."
  }
}

run "retention_is_the_callers_to_set" {
  command = plan
  module {
    source = "../../modules/audit"
  }
  variables {
    retention_days     = 30
    log_retention_days = 90
  }

  assert {
    condition     = one(one(aws_s3_bucket_object_lock_configuration.trail[0].rule).default_retention).days == 30
    error_message = "retention_days must reach the lock."
  }
  assert {
    condition     = aws_cloudwatch_log_group.trail[0].retention_in_days == 90
    error_message = "log_retention_days must reach the log group."
  }
}

run "only_this_trail_can_write_to_the_log_bucket" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  # Every allow to the CloudTrail principal, not a named one: a statement added
  # later without the condition must fail here too.
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.trail_bucket.statement : s
      if s.effect != "Deny" && contains(flatten([for p in s.principals : tolist(p.identifiers)]), "cloudtrail.amazonaws.com")
    ]) == 2
    error_message = "CloudTrail needs exactly two allows: the ACL check and the delivery."
  }
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.trail_bucket.statement :
      anytrue([
        for c in s.condition :
        c.test == "StringEquals" && c.variable == "aws:SourceArn" && c.values == tolist(["arn:aws:cloudtrail:us-east-1:123456789012:trail/t-audit"])
      ])
      if s.effect != "Deny" && contains(flatten([for p in s.principals : tolist(p.identifiers)]), "cloudtrail.amazonaws.com")
    ])
    error_message = "Every allow to cloudtrail.amazonaws.com must carry aws:SourceArn for this trail, or any account's trail can write here."
  }
  assert {
    condition     = aws_cloudtrail.this[0].name == "t-audit"
    error_message = "The ARN the bucket policy trusts must be the ARN of the trail this module creates."
  }
  assert {
    condition = flatten([
      for s in data.aws_iam_policy_document.trail_bucket.statement : tolist(s.resources)
      if s.sid == "CloudTrailDelivers"
    ]) == ["arn:aws:s3:::t-audit-trail-123456789012-us-east-1/AWSLogs/123456789012/*"]
    error_message = "Delivery is allowed under this account's prefix only."
  }
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.trail_bucket.statement : s
      if s.effect != "Deny" && length([for a in s.actions : a if !contains(["s3:GetBucketAcl", "s3:PutObject"], a)]) > 0
    ]) == 0
    error_message = "The bucket policy allows CloudTrail the ACL check and PutObject, nothing else."
  }
}

run "both_buckets_refuse_plaintext" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition = length([
      for s in data.aws_iam_policy_document.trail_bucket.statement : s
      if s.effect == "Deny" && contains(s.actions, "s3:*") && anytrue([
        for c in s.condition : c.test == "Bool" && c.variable == "aws:SecureTransport" && c.values == tolist(["false"])
      ])
    ]) == 1
    error_message = "The trail's bucket must deny every S3 action over a connection without TLS."
  }
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.config_bucket.statement : s
      if s.effect == "Deny" && contains(s.actions, "s3:*") && anytrue([
        for c in s.condition : c.test == "Bool" && c.variable == "aws:SecureTransport" && c.values == tolist(["false"])
      ])
    ]) == 1
    error_message = "The Config bucket must deny every S3 action over a connection without TLS."
  }
}

run "the_trail_also_lands_in_an_encrypted_log_group" {
  # apply: the group's ARN is computed.
  command = apply
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = aws_cloudtrail.this[0].cloud_watch_logs_group_arn == "${aws_cloudwatch_log_group.trail[0].arn}:*"
    error_message = "The trail must deliver to its log group."
  }
  assert {
    condition     = aws_cloudtrail.this[0].cloud_watch_logs_role_arn == aws_iam_role.trail_logs[0].arn
    error_message = "The trail must deliver as its own role."
  }
  assert {
    condition     = aws_cloudwatch_log_group.trail[0].kms_key_id == var.kms_key_arn
    error_message = "The log group must be encrypted with the CMK."
  }
  assert {
    condition     = aws_cloudwatch_log_group.trail[0].retention_in_days == 365
    error_message = "A year of searchable events by default."
  }
  assert {
    condition = one(data.aws_iam_policy_document.trail_logs.statement).resources == toset([
      "arn:aws:logs:us-east-1:123456789012:log-group:/cascade/t/cloudtrail:log-stream:123456789012_CloudTrail_us-east-1*"
    ])
    error_message = "The delivery role writes this trail's streams in this group, and no other."
  }
  assert {
    condition     = toset(one(data.aws_iam_policy_document.trail_logs.statement).actions) == toset(["logs:CreateLogStream", "logs:PutLogEvents"])
    error_message = "The delivery role can create a stream and put events, nothing else."
  }
}

run "guardduty_is_on" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = aws_guardduty_detector.this[0].enable
    error_message = "A detector that exists and is disabled satisfies the SCP and detects nothing."
  }
}

run "config_records_everything_and_is_actually_recording" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = one(aws_config_configuration_recorder.this[0].recording_group).all_supported
    error_message = "The recorder must record every supported type, including ones AWS adds later."
  }
  assert {
    condition     = one(aws_config_configuration_recorder.this[0].recording_group).include_global_resource_types
    error_message = "Global types are IAM: the recorder must include them."
  }
  assert {
    condition     = aws_config_configuration_recorder_status.this[0].is_enabled
    error_message = "A recorder is created stopped; it must be started."
  }
  assert {
    condition     = aws_config_delivery_channel.this[0].s3_kms_key_arn == var.kms_key_arn
    error_message = "Config history must be encrypted with the CMK."
  }
  assert {
    condition     = aws_iam_role_policy_attachment.config[0].policy_arn == "arn:aws:iam::aws:policy/service-role/AWS_ConfigRole"
    error_message = "The recorder reads configurations through AWS's managed policy."
  }
  assert {
    condition = anytrue([
      for c in one(data.aws_iam_policy_document.config_assume.statement).condition :
      c.variable == "aws:SourceAccount" && c.values == tolist(["123456789012"])
    ])
    error_message = "config.amazonaws.com may assume the role on behalf of this account only."
  }
}

run "config_history_has_its_own_bucket_because_config_cannot_write_to_a_locked_one" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = aws_config_delivery_channel.this[0].s3_bucket_name == "t-audit-config-123456789012-us-east-1"
    error_message = "AWS Config does not support delivery to a bucket with a default Object Lock retention: the channel must not point at the trail's bucket."
  }
  assert {
    condition     = aws_s3_bucket.config[0].object_lock_enabled != true
    error_message = "The Config bucket must stay deliverable: no Object Lock."
  }
  assert {
    condition     = one(aws_s3_bucket_versioning.config[0].versioning_configuration).status == "Enabled"
    error_message = "Without a lock, versioning is what keeps an overwritten or deleted history file."
  }
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.config_bucket.statement : s
      if s.effect == "Deny" && contains(s.actions, "s3:DeleteObjectVersion") && length(s.condition) == 0 &&
      contains(flatten([for p in s.principals : tolist(p.identifiers)]), "*")
    ]) == 1
    error_message = "Permanent deletion of a history version must be denied to everyone, unconditionally."
  }
  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.config[0].rule).apply_server_side_encryption_by_default).kms_master_key_id == var.kms_key_arn
    error_message = "The Config bucket must default to the CMK."
  }
  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.config[0].block_public_acls,
      aws_s3_bucket_public_access_block.config[0].block_public_policy,
      aws_s3_bucket_public_access_block.config[0].ignore_public_acls,
      aws_s3_bucket_public_access_block.config[0].restrict_public_buckets,
    ])
    error_message = "Every public-access block must be on."
  }
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.config_delivery[0].statement : s
      if contains(s.resources, "*")
    ]) == 0
    error_message = "Every allow in the delivery policy must name its resources."
  }
}

run "the_key_policy_the_caller_must_carry_is_scoped_to_this_trail_and_this_group" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.required_key_policy.statement :
      anytrue([
        for c in s.condition :
        c.test == "StringEquals" && c.variable == "aws:SourceArn" && c.values == tolist(["arn:aws:cloudtrail:us-east-1:123456789012:trail/t-audit"])
      ])
      if contains(flatten([for p in s.principals : tolist(p.identifiers)]), "cloudtrail.amazonaws.com")
    ])
    error_message = "Every key grant to CloudTrail must be scoped to this trail."
  }
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.required_key_policy.statement : s
      if contains(s.actions, "kms:GenerateDataKey*") && anytrue([
        for c in s.condition :
        c.test == "StringLike" && c.variable == "kms:EncryptionContext:aws:cloudtrail:arn" && c.values == tolist(["arn:aws:cloudtrail:*:123456789012:trail/*"])
      ]) && contains(flatten([for p in s.principals : tolist(p.identifiers)]), "cloudtrail.amazonaws.com")
    ]) == 1
    error_message = "CloudTrail's encrypt grant must carry the cloudtrail encryption-context condition."
  }
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.required_key_policy.statement : s
      if contains(flatten([for p in s.principals : tolist(p.identifiers)]), "logs.us-east-1.amazonaws.com") && anytrue([
        for c in s.condition :
        c.test == "ArnEquals" && c.variable == "kms:EncryptionContext:aws:logs:arn" && c.values == tolist(["arn:aws:logs:us-east-1:123456789012:log-group:/cascade/t/cloudtrail"])
      ])
    ]) == 1
    error_message = "The Logs grant must be the regional principal, pinned to this log group."
  }
  assert {
    condition     = aws_cloudwatch_log_group.trail[0].name == "/cascade/t/cloudtrail"
    error_message = "The group the key policy trusts must be the group this module creates."
  }
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.required_key_policy.statement : s
      if length(s.condition) == 0
    ]) == 0
    error_message = "No service principal gets the key unconditionally: the key also encrypts the event lake."
  }
}

run "the_controls_output_reads_the_resources" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition = alltrue([
      output.controls.multi_region,
      output.controls.global_service_events,
      output.controls.log_file_validation,
      output.controls.logging,
      output.controls.trail_bucket_locked,
      output.controls.buckets_private,
      output.controls.guardduty_enabled,
      output.controls.config_records_all_types,
      output.controls.config_records_global,
      output.controls.config_recording,
    ])
    error_message = "Every boolean control must be on."
  }
  assert {
    condition     = output.controls.trail_retention_mode == "GOVERNANCE" && output.controls.trail_retention_days == 365
    error_message = "The lock's mode and period, as configured."
  }
  assert {
    condition     = output.controls.trail_bucket_versioning == "Enabled" && output.controls.config_bucket_versioning == "Enabled"
    error_message = "Both buckets versioned."
  }
  assert {
    condition = output.controls.cloudtrail_allow_source_arns == {
      CloudTrailChecksTheAcl = ["arn:aws:cloudtrail:us-east-1:123456789012:trail/t-audit"]
      CloudTrailDelivers     = ["arn:aws:cloudtrail:us-east-1:123456789012:trail/t-audit"]
    }
    error_message = "Each CloudTrail allow, with the one trail it is scoped to."
  }
  assert {
    condition     = output.controls.trail_bucket_deny_sids == ["TlsOnly"] && output.controls.config_bucket_deny_sids == ["TlsOnly", "HistoryIsNeverPermanentlyDeleted"]
    error_message = "The denies each bucket policy carries."
  }
  assert {
    condition     = output.controls.event_categories == ["Management", "Data"] && output.controls.data_event_prefixes == ["arn:aws:s3:::t-audit-config-123456789012-us-east-1/"]
    error_message = "With no buckets named: management events, and data events for the Config bucket alone."
  }
  assert {
    condition     = output.controls.trail_bucket != output.controls.config_delivery_bucket
    error_message = "Config must not deliver to the locked bucket."
  }
  assert {
    condition     = output.controls.trail_kms_key_arn == var.kms_key_arn && output.controls.log_group_kms_key_arn == var.kms_key_arn && output.controls.config_delivery_encrypted == var.kms_key_arn
    error_message = "Trail, log group and Config history all under the CMK."
  }
}

run "an_object_path_or_wildcard_in_a_bucket_arn_is_refused" {
  command = plan
  module {
    source = "../../modules/audit"
  }
  variables {
    data_event_bucket_arns = ["arn:aws:s3:::t-events-x/*"]
  }
  expect_failures = [var.data_event_bucket_arns]
}

run "logging_the_log_bucket_into_itself_is_refused" {
  command = plan
  module {
    source = "../../modules/audit"
  }
  variables {
    data_event_bucket_arns = ["arn:aws:s3:::t-audit-trail-123456789012-us-east-1"]
  }
  expect_failures = [aws_cloudtrail.this]
}

run "a_log_retention_cloudwatch_would_reject_is_refused_before_apply" {
  command = plan
  module {
    source = "../../modules/audit"
  }
  variables {
    log_retention_days = 100
  }
  expect_failures = [var.log_retention_days]
}

# --- GuardDuty findings go somewhere (ADR-0042) -----------------------------------------------

run "findings_at_or_above_the_threshold_go_to_the_alerts_topic" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = tolist(jsondecode(aws_cloudwatch_event_rule.findings[0].event_pattern).source) == tolist(["aws.guardduty"]) && tolist(jsondecode(aws_cloudwatch_event_rule.findings[0].event_pattern)["detail-type"]) == tolist(["GuardDuty Finding"])
    error_message = "The rule matches GuardDuty findings and nothing else."
  }
  assert {
    condition     = tolist(jsondecode(aws_cloudwatch_event_rule.findings[0].event_pattern).detail.severity[0].numeric) == tolist([">=", 7])
    error_message = "At or above the caller's threshold: a numeric >= match, not a list of severities somebody typed."
  }
  assert {
    condition     = aws_cloudwatch_event_target.findings[0].arn == var.alerts_topic_arn
    error_message = "The target is the alerts topic."
  }
  assert {
    condition     = length(aws_cloudwatch_event_target.findings[0].input_transformer) == 1 && strcontains(one(aws_cloudwatch_event_target.findings[0].input_transformer).input_template, "<severity>") && strcontains(one(aws_cloudwatch_event_target.findings[0].input_transformer).input_template, "<id>")
    error_message = "The message names the severity and the finding id, so a person can act on it without the console."
  }
  assert {
    condition     = aws_guardduty_detector.this[0].finding_publishing_frequency == "FIFTEEN_MINUTES"
    error_message = "Updates to a finding reach EventBridge at the shortest interval GuardDuty offers."
  }
}

run "the_threshold_follows_the_caller" {
  command = plan
  module {
    source = "../../modules/audit"
  }
  variables {
    guardduty_min_severity = 4
  }

  assert {
    condition     = tolist(jsondecode(aws_cloudwatch_event_rule.findings[0].event_pattern).detail.severity[0].numeric) == tolist([">=", 4])
    error_message = "The pattern must be built from the variable, not restated."
  }
}

run "the_topic_policy_admits_this_rule_and_this_alarm_by_arn" {
  # apply, not plan: the statements name ARNs built from data sources.
  command = apply
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = tolist(output.controls.topic_allow_source_arns["GuardDutyFindingsRulePublishes"]) == tolist(["arn:aws:events:us-east-1:123456789012:rule/t-guardduty-findings"])
    error_message = "EventBridge's allow is scoped by aws:SourceArn to exactly this rule; events.amazonaws.com is every account's principal."
  }
  assert {
    condition     = endswith(output.controls.topic_allow_source_arns["GuardDutyFindingsRulePublishes"][0], "rule/${output.controls.findings_rule_name}")
    error_message = "And the ARN in the statement names the rule that was actually created."
  }
  assert {
    condition     = tolist(output.controls.topic_allow_source_arns["GuardDutyDeliveryAlarmPublishes"]) == tolist(["arn:aws:cloudwatch:us-east-1:123456789012:alarm:t-guardduty-alert-delivery"])
    error_message = "The delivery alarm's allow is scoped to that one alarm."
  }
  assert {
    condition     = alltrue([for sid, arns in output.controls.topic_allow_source_arns : length(arns) == 1])
    error_message = "Every allow to a service principal carries exactly one aws:SourceArn."
  }
  assert {
    condition     = tolist(output.controls.topic_allow_resources) == tolist([var.alerts_topic_arn])
    error_message = "Both statements are for the one topic."
  }
}

run "a_rule_that_cannot_deliver_alarms" {
  command = plan
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.findings_delivery[0].metric_name == "FailedInvocations" && aws_cloudwatch_metric_alarm.findings_delivery[0].namespace == "AWS/Events"
    error_message = "A matched finding that EventBridge could not publish is the failure that would otherwise be silent."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.findings_delivery[0].dimensions.RuleName == aws_cloudwatch_event_rule.findings[0].name
    error_message = "For this rule."
  }
  assert {
    condition     = tolist(aws_cloudwatch_metric_alarm.findings_delivery[0].alarm_actions) == tolist([var.alerts_topic_arn]) && aws_cloudwatch_metric_alarm.findings_delivery[0].threshold == 1
    error_message = "One failed invocation alerts."
  }
}

run "a_severity_off_the_scale_is_refused" {
  command = plan
  module {
    source = "../../modules/audit"
  }
  variables {
    guardduty_min_severity = 11
  }
  expect_failures = [var.guardduty_min_severity]
}

run "a_topic_that_is_not_a_topic_is_refused" {
  command = plan
  module {
    source = "../../modules/audit"
  }
  variables {
    alerts_topic_arn = "t-cost-alerts"
  }
  expect_failures = [var.alerts_topic_arn]
}

# --- An account whose trail was made outside Terraform ----------------------------------------

run "an_account_with_its_own_trail_gets_no_second_one" {
  command = apply
  module {
    source = "../../modules/audit"
  }
  variables {
    create_trail = false
  }

  assert {
    condition     = length(aws_cloudtrail.this) == 0 && length(aws_s3_bucket.trail) == 0 && length(aws_s3_bucket_policy.trail) == 0 && length(aws_cloudwatch_log_group.trail) == 0 && length(aws_iam_role.trail_logs) == 0 && length(aws_iam_role_policy.trail_logs) == 0
    error_message = "With create_trail off nothing of the trail is made: no trail, no bucket, no log group, no role."
  }
  assert {
    condition     = output.trail_arn == null && output.log_bucket == null && !output.controls.trail_created && output.controls.multi_region == null
    error_message = "The outputs say 'not ours' with nulls, never with the values of a trail that does not exist."
  }
  assert {
    condition     = length(output.controls.data_event_prefixes) == 0 && length(output.controls.trail_bucket_deny_sids) == 0 && length(output.controls.cloudtrail_allow_source_arns) == 0
    error_message = "No trail of this module's records data events, and the output must not claim a bucket policy that was never attached."
  }
  assert {
    condition     = length(output.controls.key_policy_statement_ids) == 0
    error_message = "A key grant to CloudTrail for a trail name nobody created is a grant to whoever creates it."
  }
  assert {
    condition     = output.controls.guardduty_enabled && output.controls.config_recording && output.controls.config_records_all_types && output.config_bucket == "t-audit-config-123456789012-us-east-1"
    error_message = "GuardDuty and Config do not go with the trail."
  }
  assert {
    condition     = output.controls.buckets_private
    error_message = "The Config bucket stays private on its own."
  }
}

run "with_the_trail_on_the_key_policy_carries_its_three_statements" {
  command = apply
  module {
    source = "../../modules/audit"
  }

  assert {
    condition     = output.controls.trail_created && toset(output.controls.key_policy_statement_ids) == toset(["CloudTrailEncryptsTheTrail", "CloudTrailDescribesTheKey", "CloudWatchLogsEncryptsTheTrailGroup"])
    error_message = "The default is the trail, with the three key statements it needs."
  }
}

# --- A deployment nobody operates (ADR-0054) ----------------------------------------------

run "without_guardduty_there_is_no_detector_no_rule_and_no_grant_on_the_topic" {
  command = apply
  module {
    source = "../../modules/audit"
  }
  variables {
    enable_guardduty = false
  }

  assert {
    condition     = length(aws_guardduty_detector.this) == 0 && length(aws_cloudwatch_event_rule.findings) == 0 && length(aws_cloudwatch_event_target.findings) == 0 && length(aws_cloudwatch_metric_alarm.findings_delivery) == 0
    error_message = "With GuardDuty off there is no detector and nothing that routes its findings."
  }
  assert {
    condition     = output.detector_id == null && !output.controls.guardduty_created && output.controls.guardduty_enabled == null && output.controls.findings_pattern == null && output.controls.findings_delivery_alarm == null
    error_message = "The outputs say 'not ours' with nulls."
  }
  assert {
    condition     = length(output.controls.topic_allow_source_arns) == 0 && length(output.controls.topic_allow_resources) == 0
    error_message = "A publish grant for a rule name nobody created is a grant to whoever creates it."
  }
  assert {
    condition     = output.controls.trail_created && output.controls.config_created && output.controls.config_recording
    error_message = "The trail and Config do not go with GuardDuty."
  }
}

run "without_config_there_is_no_recorder_no_bucket_and_no_selector_for_one" {
  command = apply
  module {
    source = "../../modules/audit"
  }
  variables {
    enable_config = false
  }

  assert {
    condition     = length(aws_config_configuration_recorder.this) == 0 && length(aws_config_delivery_channel.this) == 0 && length(aws_config_configuration_recorder_status.this) == 0 && length(aws_s3_bucket.config) == 0 && length(aws_s3_bucket_policy.config) == 0 && length(aws_iam_role.config) == 0 && length(aws_iam_role_policy.config_delivery) == 0 && length(aws_iam_role_policy_attachment.config) == 0
    error_message = "With Config off there is no recorder, channel, role or history bucket."
  }
  assert {
    condition     = output.config_bucket == null && !output.controls.config_created && output.controls.config_recording == null && output.controls.config_records_all_types == null && length(output.controls.config_bucket_deny_sids) == 0
    error_message = "The outputs say 'not ours' with nulls, and claim no bucket policy that was never attached."
  }
  assert {
    condition     = length(output.controls.data_event_prefixes) == 0
    error_message = "The trail always records the Config bucket; with no such bucket it must not carry a selector for the name."
  }
  assert {
    condition     = output.controls.trail_created && output.controls.guardduty_created && output.controls.buckets_private
    error_message = "The trail and GuardDuty do not go with Config."
  }
}

run "with_all_three_off_the_module_makes_nothing_and_claims_nothing" {
  command = apply
  module {
    source = "../../modules/audit"
  }
  variables {
    create_trail     = false
    enable_guardduty = false
    enable_config    = false
  }

  assert {
    condition     = !output.controls.trail_created && !output.controls.guardduty_created && !output.controls.config_created && output.trail_arn == null && output.detector_id == null && output.config_bucket == null
    error_message = "Nothing is made."
  }
  assert {
    condition     = output.controls.buckets_private == null
    error_message = "'Every bucket is private' over no buckets is vacuously true; the output must be null instead."
  }
  assert {
    condition     = length(output.controls.key_policy_statement_ids) == 0 && length(output.controls.topic_allow_source_arns) == 0
    error_message = "No grant on the caller's key or topic survives the things it was for."
  }
}
