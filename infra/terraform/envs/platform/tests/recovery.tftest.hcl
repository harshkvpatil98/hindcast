# Tier-0 recovery, offline: locked, replicated, and closed to the simulation.
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

# Tested through the root: see the `controls` output in modules/recovery.
variables {
  region                       = "us-east-1"
  replica_region               = "us-west-2"
  infrastructure_allowance_usd = 50
  guardduty_min_severity       = 7
}

run "both_buckets_are_locked_in_different_regions" {
  command = plan

  assert {
    condition     = module.recovery.controls.primary_locked && module.recovery.controls.replica_locked
    error_message = "Primary and replica must both have Object Lock."
  }
  assert {
    condition     = endswith(module.recovery.controls.primary_bucket, "us-east-1") && endswith(module.recovery.controls.replica_bucket, "us-west-2")
    error_message = "The replica must live in the second region."
  }
}

run "a_delete_in_the_primary_does_not_reach_the_replica" {
  command = plan

  assert {
    condition     = module.recovery.controls.delete_markers_replicate == "Disabled"
    error_message = "Replicating delete markers would carry an accidental deletion to the copy that exists to survive it."
  }
}

run "the_simulation_is_denied_the_labels_archive" {
  # apply, not plan: the Deny's resources are built from the bucket's ARN.
  command = apply
  variables {
    simulation_principal_arns = ["arn:aws:iam::123456789012:role/cascade-sim"]
  }

  assert {
    condition     = contains(module.recovery.controls.deny_statements, "SimulationNeverReadsLabels")
    error_message = "Invariant 2 on a bucket: an explicit Deny on registry/* for the simulation's principals."
  }
  assert {
    condition     = contains(module.recovery.controls.labels_deny_actions, "s3:GetObject")
    error_message = "The Deny must cover reading the archive."
  }
  assert {
    condition     = toset(module.recovery.controls.labels_deny_resources) == toset(["arn:aws:s3:::mock/registry/*", "arn:aws:s3:::mock/source-cache/*"])
    error_message = "Both places the labels are: the registry archive and the source cache, whose raw market responses state how each market resolved. Not the LLM cache, which the simulation itself wrote."
  }
}

run "with_no_simulation_principal_there_is_no_empty_deny" {
  command = plan

  assert {
    condition     = !contains(module.recovery.controls.deny_statements, "SimulationNeverReadsLabels")
    error_message = "A Deny with no principals is an invalid policy; it must be omitted."
  }
  assert {
    condition     = contains(module.recovery.controls.deny_statements, "TlsOnly")
    error_message = "The TLS-only Deny is unconditional."
  }
}

# --- The caches are in the plan (ADR-0042) -----------------------------------------------

run "the_three_tier_zero_prefixes_are_named_once_and_all_replicate" {
  command = plan

  assert {
    condition     = module.recovery.prefixes == { registry = "registry", source_cache = "source-cache", llm_cache = "llm-cache" }
    error_message = "Three datasets, three prefixes, defined in one place."
  }
  assert {
    condition     = module.recovery.controls.replication_prefix == ""
    error_message = "The replication rule carries every prefix: a rule filtered to registry/ would leave the caches single-region."
  }
  assert {
    condition     = endswith(module.recovery.llm_cache_prefix, "/llm-cache/") && endswith(module.recovery.source_cache_prefix, "/source-cache/")
    error_message = "Writers are told where to go."
  }
}

run "the_archivist_can_add_to_the_two_uploaded_prefixes_and_read_nothing_back" {
  command = apply

  assert {
    condition     = length(setintersection(toset(module.recovery.controls.archive_write_actions), toset(["s3:GetObject", "s3:GetObjectVersion", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention"]))) == 0
    error_message = "A laptop credential that can only add: no read of the labels, no delete, no lock override."
  }
  assert {
    condition     = contains(module.recovery.controls.archive_write_actions, "s3:PutObject")
    error_message = "It can add."
  }
  assert {
    condition     = toset(module.recovery.controls.archive_write_objects) == toset(["arn:aws:s3:::mock/registry/*", "arn:aws:s3:::mock/source-cache/*"])
    error_message = "Under the registry and source-cache prefixes only; the LLM cache is DataSync's, from the sandbox."
  }
}
