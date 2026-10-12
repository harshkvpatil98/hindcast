# The durable LLM cache, offline (ADR-0042): encrypted, reached one way, and
# copied -- add-only -- into the platform's tier-0 bucket.
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
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock" }
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
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:mock" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/mock" }
  }
  mock_resource "aws_cloudwatch_metric_alarm" {
    defaults = { arn = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:mock" }
  }
  mock_resource "aws_efs_file_system" {
    defaults = { arn = "arn:aws:elasticfilesystem:us-east-1:123456789012:file-system/fs-mock" }
  }
  mock_resource "aws_efs_access_point" {
    defaults = { arn = "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-mock" }
  }
  mock_resource "aws_datasync_location_efs" {
    defaults = { arn = "arn:aws:datasync:us-east-1:123456789012:location/loc-efs" }
  }
  mock_resource "aws_datasync_location_s3" {
    defaults = { arn = "arn:aws:datasync:us-east-1:123456789012:location/loc-s3" }
  }
  mock_resource "aws_datasync_task" {
    defaults = { arn = "arn:aws:datasync:us-east-1:123456789012:task/task-mock" }
  }
  mock_resource "aws_eip" {
    defaults = { public_ip = "203.0.113.10" }
  }
}

mock_provider "random" {}

variables {
  name                      = "t"
  vpc_id                    = "vpc-0123"
  subnet_ids                = ["subnet-0aaa", "subnet-0bbb"]
  kms_key_arn               = "arn:aws:kms:us-east-1:123456789012:key/platform"
  client_security_group_ids = { study = "sg-study" }
  recovery_bucket_arn       = "arn:aws:s3:::cascade-recovery-123456789012-us-east-1"
  recovery_prefix           = "llm-cache"
  recovery_kms_key_arn      = "arn:aws:kms:us-east-1:123456789012:key/recovery"
  sync_schedule_expression  = "rate(1 hour)"
  alerts_topic_arn          = "arn:aws:sns:us-east-1:123456789012:cascade-cost-alerts"
}

run "the_file_system_is_encrypted_with_the_cmk" {
  command = plan
  module {
    source = "../../modules/cache"
  }

  assert {
    condition     = aws_efs_file_system.this.encrypted && aws_efs_file_system.this.kms_key_id == var.kms_key_arn
    error_message = "Every recording is a paid model call; the file system is encrypted, under the platform CMK."
  }
  assert {
    condition     = aws_efs_file_system.this.throughput_mode == "elastic"
    error_message = "Elastic throughput bills per byte moved and nothing while idle."
  }
  assert {
    condition     = one(aws_efs_backup_policy.this.backup_policy).status == "DISABLED"
    error_message = "Automatic EFS backups are off, explicitly: the recovery bucket is the backup, and a second unlocked copy is not."
  }
}

run "one_identity_one_root_and_three_refusals" {
  command = apply
  module {
    source = "../../modules/cache"
  }

  assert {
    condition     = output.controls.access_point_uid == 10001 && one(aws_efs_access_point.llm.posix_user).gid == 10001
    error_message = "The access point imposes the image's uid on every client, whatever the container says it is."
  }
  assert {
    condition     = output.controls.access_point_root == "/llm"
    error_message = "Clients see /llm as their root and cannot walk above it."
  }
  assert {
    condition     = toset(output.controls.file_system_deny_sids) == toset(["TlsOnly", "OnlyThroughAnAccessPoint", "NobodyIsRoot"])
    error_message = "The file system policy denies plaintext NFS, any mount not through the access point, and root -- whatever an identity policy grants."
  }
}

run "one_mount_target_per_subnet_and_nfs_from_named_clients_only" {
  command = plan
  module {
    source = "../../modules/cache"
  }

  assert {
    condition     = length(aws_efs_mount_target.this) == length(var.subnet_ids)
    error_message = "A client resolves the file system to the mount target in its own AZ; every isolated subnet gets one."
  }
  assert {
    condition     = toset(keys(aws_vpc_security_group_ingress_rule.nfs)) == toset(["study", "datasync"])
    error_message = "NFS is admitted from the named clients and this module's own DataSync group, and from nobody else."
  }
  assert {
    condition     = alltrue([for r in aws_vpc_security_group_ingress_rule.nfs : r.from_port == 2049 && r.to_port == 2049 && r.ip_protocol == "tcp"])
    error_message = "NFS only."
  }
}

run "the_archive_job_adds_never_deletes_never_overwrites_and_is_basic" {
  command = apply
  module {
    source = "../../modules/cache"
  }

  assert {
    condition     = output.controls.sync_task_mode == "BASIC"
    error_message = "Enhanced mode bills per execution; hourly, that fee alone would exceed the study's model budget in a month."
  }
  assert {
    condition     = !output.controls.sync_deletes_from_archive && !output.controls.sync_overwrites
    error_message = "The archive only grows: an entry deleted from the file system stays archived, and an existing object is by definition the same answer."
  }
  assert {
    condition     = tolist(output.controls.sync_excludes) == tolist(["*.tmp"])
    error_message = "cache.py's half-written temporaries must never reach a bucket where they could not be deleted."
  }
  assert {
    condition     = one(aws_datasync_task.archive.options).verify_mode == "ONLY_FILES_TRANSFERRED"
    error_message = "Verifying the whole archive on every run would GET every object every hour."
  }
  assert {
    condition     = output.controls.sync_schedule == var.sync_schedule_expression
    error_message = "The schedule is the caller's RPO decision, applied unchanged."
  }
  assert {
    condition     = output.controls.sync_in_transit == "TLS1_2"
    error_message = "DataSync mounts with TLS, or the file system policy refuses it."
  }
  assert {
    condition     = output.controls.archive_subdirectory == "/llm-cache/t/"
    error_message = "One directory per sandbox under the recovery prefix; two sandboxes must not interleave."
  }
}

run "the_writer_can_add_under_one_prefix_and_delete_nothing" {
  command = apply
  module {
    source = "../../modules/cache"
  }

  assert {
    condition     = !contains(output.controls.archive_write_actions, "s3:DeleteObject") && !contains(output.controls.archive_write_actions, "s3:DeleteObjectVersion")
    error_message = "AWS's documented destination policy includes s3:DeleteObject; here it is withheld -- tier 0 is the bucket nothing in a sandbox may remove from."
  }
  assert {
    condition     = contains(output.controls.archive_write_actions, "s3:PutObject")
    error_message = "It can add."
  }
  assert {
    condition     = tolist(output.controls.archive_write_resources) == tolist(["${var.recovery_bucket_arn}/llm-cache/t/*"])
    error_message = "Objects under this sandbox's directory only: not registry/, where the labels are, and not another sandbox's."
  }
  assert {
    condition     = tolist(output.controls.sync_reader_actions) == tolist(["elasticfilesystem:ClientMount"])
    error_message = "The copy job mounts read-only: no ClientWrite, so it cannot alter what it copies."
  }
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.datasync_assume.statement :
      length([for c in s.condition : c if c.variable == "aws:SourceAccount"]) == 1 && length([for c in s.condition : c if c.variable == "aws:SourceArn"]) == 1
    ])
    error_message = "datasync.amazonaws.com is every account's principal; the trust is conditioned on this account's tasks."
  }
}

run "a_failed_copy_is_announced_under_the_name_the_platform_admits" {
  command = plan
  module {
    source = "../../modules/cache"
  }

  assert {
    condition     = tolist(output.controls.sync_failure_rules) == tolist(["t-task-llm-cache-sync-failed"])
    error_message = "The rule is named <sandbox>-task-... so the platform root's existing topic-policy statement admits it."
  }
  assert {
    condition     = tolist(jsondecode(aws_cloudwatch_event_rule.sync_failed[0].event_pattern).detail.State) == tolist(["ERROR"]) && tolist(jsondecode(aws_cloudwatch_event_rule.sync_failed[0].event_pattern).source) == tolist(["aws.datasync"])
    error_message = "It matches DataSync executions that ended in ERROR."
  }
  assert {
    condition     = aws_cloudwatch_event_target.sync_failed[0].arn == var.alerts_topic_arn
    error_message = "And publishes to the alerts topic."
  }
}

run "with_no_topic_there_is_no_rule_and_no_dangling_target" {
  command = plan
  module {
    source = "../../modules/cache"
  }
  variables {
    alerts_topic_arn = null
  }

  assert {
    condition     = length(aws_cloudwatch_event_rule.sync_failed) == 0 && length(aws_cloudwatch_event_target.sync_failed) == 0
    error_message = "A sandbox can exist before the platform root that owns the topic."
  }
}

run "a_sub_hourly_schedule_is_refused" {
  command = plan
  module {
    source = "../../modules/cache"
  }
  variables {
    sync_schedule_expression = "rate(5 minutes)"
  }
  expect_failures = [var.sync_schedule_expression]
}

run "a_bucket_arn_with_a_path_is_refused" {
  command = plan
  module {
    source = "../../modules/cache"
  }
  variables {
    recovery_bucket_arn = "arn:aws:s3:::cascade-recovery/llm-cache"
  }
  expect_failures = [var.recovery_bucket_arn]
}
