# The event lake, offline: invariant 6 (append-only) as AWS controls.
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
}

variables {
  name          = "t"
  bucket_suffix = "123456789012-us-east-1"
  kms_key_arn   = "arn:aws:kms:us-east-1:123456789012:key/mock"
}

run "the_log_is_locked_versioned_and_private" {
  command = plan
  module {
    source = "../../modules/eventlake"
  }

  assert {
    condition     = aws_s3_bucket.events.object_lock_enabled
    error_message = "The events bucket must have Object Lock: the log is append-only (invariant 6)."
  }
  assert {
    condition     = one(one(aws_s3_bucket_object_lock_configuration.events.rule).default_retention).days == 365
    error_message = "Every object must get a default retention."
  }
  assert {
    condition     = one(aws_s3_bucket_versioning.events.versioning_configuration).status == "Enabled"
    error_message = "Object Lock requires versioning."
  }
  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.events.block_public_acls,
      aws_s3_bucket_public_access_block.events.block_public_policy,
      aws_s3_bucket_public_access_block.events.ignore_public_acls,
      aws_s3_bucket_public_access_block.events.restrict_public_buckets,
    ])
    error_message = "Every public-access block must be on."
  }
}

run "the_writer_can_append_and_is_explicitly_denied_everything_destructive" {
  command = plan
  module {
    source = "../../modules/eventlake"
  }

  assert {
    condition = length([
      for s in data.aws_iam_policy_document.writer.statement : s
      if s.effect == "Deny" && length(setsubtract(
        ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention", "s3:PutObjectRetention"],
        s.actions
      )) == 0
    ]) == 1
    error_message = "The writer must carry an explicit Deny on delete and on every lock override."
  }
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.writer.statement : s
      if s.effect != "Deny" && length([for a in s.actions : a if startswith(a, "s3:") && a != "s3:PutObject"]) > 0
    ]) == 0
    error_message = "The writer's only S3 permission is PutObject."
  }
}

run "no_statement_in_either_role_is_granted_on_everything" {
  # apply, not plan: the resource lists hold ARNs that are unknown until then.
  command = apply
  module {
    source = "../../modules/eventlake"
  }

  assert {
    condition = length([
      for s in concat(data.aws_iam_policy_document.writer.statement, data.aws_iam_policy_document.analyst.statement) : s
      if s.effect != "Deny" && contains(s.resources, "*")
    ]) == 0
    error_message = "Every allow in the lake's roles must name its resources."
  }
}

run "queries_cannot_escape_the_workgroup_settings" {
  command = plan
  module {
    source = "../../modules/eventlake"
  }

  assert {
    condition     = one(aws_athena_workgroup.this.configuration).enforce_workgroup_configuration
    error_message = "Workgroup settings must override client settings."
  }
  assert {
    condition     = one(aws_athena_workgroup.this.configuration).bytes_scanned_cutoff_per_query == 10737418240
    error_message = "Every query must carry a scan limit."
  }
  assert {
    condition     = one(one(one(aws_athena_workgroup.this.configuration).result_configuration).encryption_configuration).encryption_option == "SSE_KMS"
    error_message = "Query results must be encrypted with the CMK."
  }
}

run "the_catalog_matches_the_events_table" {
  command = plan
  module {
    source = "../../modules/eventlake"
  }

  assert {
    condition = [for c in one(aws_glue_catalog_table.events.storage_descriptor).columns : c.name] == [
      "run_id", "step", "seq", "actor_id", "obs_hash", "action", "caused_by",
      "factor_delta", "cache_hit", "tokens_in", "tokens_out", "latency_ms", "coercion",
    ]
    error_message = "The lake's columns must be migration 009's, in order."
  }
  assert {
    condition     = [for k in aws_glue_catalog_table.events.partition_keys : k.name] == ["config_id"]
    error_message = "Partitioned by ablation cell."
  }
}

run "compliance_mode_is_available_but_never_the_default" {
  command = plan
  module {
    source = "../../modules/eventlake"
  }

  assert {
    condition     = one(one(aws_s3_bucket_object_lock_configuration.events.rule).default_retention).mode == "GOVERNANCE"
    error_message = "COMPLIANCE cannot be undone by anyone; it must be chosen, not inherited."
  }
}
