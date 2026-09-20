# Publishing the study's deliverable, offline (ADR-0046): a versioned,
# encrypted, Object-Locked bucket that nothing serves publicly, written by the
# study task and read by a named principal -- never by the simulation, because
# a report carries the labels.
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

# Tested both ways: the module directly, where its resources are in reach, and
# through the root, where the wiring between the platform's parts is.
variables {
  # Root.
  region                       = "us-east-1"
  replica_region               = "us-west-2"
  infrastructure_allowance_usd = 50
  guardduty_min_severity       = 7
  inventory_schedule           = "Weekly"
  # Shared by the root and the module.
  name                       = "t"
  report_lock_retention_days = 365
  # Module only.
  bucket_suffix = "123456789012-us-east-1"
  kms_key_arn   = "arn:aws:kms:us-east-1:123456789012:key/platform"
}

run "the_reports_bucket_is_private_encrypted_versioned_and_locked" {
  command = apply
  module {
    source = "../../modules/reports"
  }

  assert {
    condition     = alltrue(output.controls.public_blocks)
    error_message = "All four public-access blocks. Nothing about a study report is published to the open web (ADR-0046)."
  }
  assert {
    condition     = output.controls.object_ownership == "BucketOwnerEnforced"
    error_message = "ACLs are disabled: the bucket owner owns every object, so no ACL can open one."
  }
  assert {
    condition     = output.controls.encryption_key == var.kms_key_arn
    error_message = "Encrypted under the platform CMK, not S3-managed keys."
  }
  assert {
    condition     = output.controls.versioning == "Enabled"
    error_message = "A report is a claim; the history of what was claimed must be complete."
  }
  assert {
    condition     = output.controls.locked && output.controls.retention_mode == "GOVERNANCE" && output.controls.retention_days == var.report_lock_retention_days
    error_message = "Object Lock in GOVERNANCE mode, for the retention the caller set. COMPLIANCE cannot be undone by anyone, including the root user."
  }
}

run "nothing_serves_this_bucket_to_an_anonymous_viewer" {
  command = apply
  module {
    source = "../../modules/reports"
  }

  assert {
    condition     = length(output.controls.allow_service_principals) == 0
    error_message = "CloudFront's service principal is not admitted, because there is no distribution: a static site would be a standing public endpoint for a directory whose CSVs carry the labels (ADR-0046)."
  }
  assert {
    condition     = contains(output.controls.deny_statements, "TlsOnly")
    error_message = "Plaintext HTTP is refused unconditionally."
  }
}

run "no_version_is_ever_removed_and_no_lock_is_ever_lifted" {
  command = apply
  module {
    source = "../../modules/reports"
  }

  assert {
    condition = length(setsubtract(
      toset(["s3:DeleteObjectVersion", "s3:BypassGovernanceRetention", "s3:PutObjectRetention", "s3:PutObjectLegalHold", "s3:PutBucketObjectLockConfiguration"]),
      toset(output.controls.immutability_deny_actions),
    )) == 0
    error_message = "GOVERNANCE's escape hatch is denied in the bucket policy to every principal: withdrawing a published figure costs two acts, and the trail records both."
  }
}

run "the_study_task_may_publish_a_report_and_may_not_read_one" {
  # apply, not plan: the Deny's resources are built from the bucket's ARN.
  command = apply
  module {
    source = "../../modules/reports"
  }
  variables {
    simulation_principal_arns = ["arn:aws:iam::123456789012:role/t-study-task"]
  }

  assert {
    condition     = contains(output.controls.deny_statements, "SimulationNeverReadsReports")
    error_message = "Invariant 2 on a bucket: baselines.csv and ablation_grid.csv carry an `outcome` column per scenario, so a report is label-bearing."
  }
  assert {
    condition     = toset(output.controls.labels_deny_actions) == toset(["s3:GetObject", "s3:GetObjectVersion"])
    error_message = "The Deny covers reading, not writing: the study task is the process that writes reports."
  }
  assert {
    condition     = tolist(output.controls.labels_deny_resources) == tolist(["arn:aws:s3:::mock/reports/*"])
    error_message = "Everything under the reports prefix."
  }
  assert {
    condition     = tolist(output.controls.labels_deny_principals) == tolist(["arn:aws:iam::123456789012:role/t-study-task"])
    error_message = "The writer is the principal denied the read. Write-only is the design, not an accident of scoping."
  }
}

run "with_no_simulation_principal_there_is_no_empty_deny" {
  command = apply
  module {
    source = "../../modules/reports"
  }

  assert {
    condition     = !contains(output.controls.deny_statements, "SimulationNeverReadsReports")
    error_message = "A Deny with no principals is an invalid policy; it must be omitted."
  }
}

run "a_reader_can_read_and_do_nothing_else" {
  command = apply
  module {
    source = "../../modules/reports"
  }

  assert {
    condition = length(setintersection(
      toset(output.controls.read_policy_actions),
      toset(["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention", "s3:PutObjectRetention"]),
    )) == 0
    error_message = "A reader cannot add a report, amend one, or remove a version."
  }
  assert {
    condition     = contains(output.controls.read_policy_actions, "s3:GetObject") && contains(output.controls.read_policy_actions, "kms:Decrypt")
    error_message = "It can read, and decrypt what it reads: `aws s3 sync`, or `aws s3 presign` for one file."
  }
  assert {
    condition     = tolist(output.controls.read_policy_objects) == tolist(["arn:aws:s3:::mock/reports/*"])
    error_message = "Under the reports prefix only."
  }
}

run "a_retention_of_zero_days_is_refused" {
  command = plan
  module {
    source = "../../modules/reports"
  }
  variables {
    report_lock_retention_days = 0
  }
  expect_failures = [var.report_lock_retention_days]
}

# --- Through the root: the wiring between the platform's parts ---------------------

run "the_platform_publishes_reports_and_the_trail_records_who_reads_them" {
  command = apply

  assert {
    condition     = endswith(module.reports.uri, "/reports/")
    error_message = "The root exports where reports go, so a sandbox is told rather than guessing."
  }
  assert {
    condition = alltrue([
      for bucket in [module.reports.bucket, module.recovery.inventory_bucket] :
      contains(module.audit.controls.data_event_prefixes, "arn:aws:s3:::${bucket}/")
    ])
    error_message = "The reports and inventory buckets carry no S3 access logging on purpose; CloudTrail data events on a locked trail are their access record -- and the only record of the two acts that could withdraw a report."
  }
  assert {
    condition     = module.reports.publish.prefix == "reports" && module.reports.publish.kms_key_arn == aws_kms_key.platform.arn
    error_message = "`reports_publish` is passed whole to the sandbox, so the two roots cannot spell the prefix differently."
  }
}

run "a_sub_daily_inventory_schedule_is_refused" {
  command = plan
  variables {
    inventory_schedule = "Hourly"
  }
  expect_failures = [var.inventory_schedule]
}
