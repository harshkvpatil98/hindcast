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
  report_lock_retention_days   = 365
  inventory_schedule           = "Weekly"
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
    condition = toset([
      for resource in module.recovery.controls.archive_write_objects :
      regex("^arn:aws:s3:::mock/([a-z-]+)/", resource)[0]
    ]) == toset(["registry", "source-cache"])
    error_message = "Under the registry and source-cache prefixes only; the LLM cache is DataSync's, from the sandbox. (The key's shape within each prefix is asserted separately.)"
  }
}

# --- The key names the seal (ADR-0046) ------------------------------------------------

run "an_upload_must_land_under_the_manifest_sha_it_belongs_to" {
  command = apply

  assert {
    condition = length(module.recovery.controls.archive_write_objects) == 2 && alltrue([
      for resource in module.recovery.controls.archive_write_objects :
      can(regex("^arn:aws:s3:::mock/(registry|source-cache)/[?]{64}[*]$", resource))
    ])
    error_message = "Both archives go under a 64-character first segment -- the sealed manifest's sha256 -- so \"is THIS split archived?\" is a prefix listing. A runbook sentence is not a mechanism; the resource ARN is."
  }
}

# --- The restore drill (ADR-0046) -----------------------------------------------------

run "the_restore_role_reads_tier_zero_and_writes_nothing_at_all_by_default" {
  command = apply

  assert {
    condition     = length(module.recovery.controls.restore_written_resources) == 0
    error_message = "With no restore target named the role is strictly read-only: the documented restore is to a machine, which is where this project's one real loss happened."
  }
  assert {
    condition = length(setintersection(
      toset(module.recovery.controls.restore_actions),
      toset(["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention", "s3:PutObjectRetention", "s3:PutBucketPolicy"]),
    )) == 0
    error_message = "A restore that can overwrite its own source is not a restore."
  }
  assert {
    condition     = contains(module.recovery.controls.restore_actions, "s3:GetObjectVersion") && contains(module.recovery.controls.restore_actions, "kms:Decrypt")
    error_message = "It can read versions in either region, and decrypt them: a region-loss drill restores from the replica."
  }
}

run "the_archive_itself_denies_the_restore_role_every_way_of_writing" {
  command = apply

  assert {
    condition     = contains(module.recovery.controls.deny_statements, "RestoreNeverWritesTheArchive")
    error_message = "The identity policy and the precondition both live in files a later change could widen; a Deny in the resource policy outranks whatever they say."
  }
  assert {
    condition = length(setsubtract(
      toset(["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention", "s3:PutBucketPolicy", "s3:PutBucketObjectLockConfiguration"]),
      toset(module.recovery.controls.restore_denied_actions),
    )) == 0
    error_message = "Adding, removing, lifting the lock and rewriting the policy are all denied to the restore role."
  }
  assert {
    condition     = length(module.recovery.controls.restore_denied_principals) == 1
    error_message = "The Deny names the restore role and nobody else; a Deny aimed at everyone would stop replication and the archivist too."
  }
}

run "a_named_restore_target_is_the_only_thing_the_role_may_write" {
  command = apply
  variables {
    restore_targets = {
      bucket_arns  = ["arn:aws:s3:::cascade-sandbox-artifacts-123456789012-us-east-1"]
      kms_key_arns = ["arn:aws:kms:us-east-1:123456789012:key/sandbox"]
    }
  }

  assert {
    condition     = tolist(module.recovery.controls.restore_write_targets) == tolist(["arn:aws:s3:::cascade-sandbox-artifacts-123456789012-us-east-1/*"])
    error_message = "The write half is exactly the named targets, and appears only when one is named."
  }
  assert {
    condition = length([
      for resource in module.recovery.controls.restore_written_resources :
      resource if startswith(resource, "arn:aws:s3:::mock")
    ]) == 0
    error_message = "No recovery bucket is among the resources this role may write."
  }
}

run "a_recovery_bucket_may_never_be_named_as_a_restore_target" {
  command = plan
  variables {
    restore_targets = {
      bucket_arns  = ["arn:aws:s3:::cascade-recovery-123456789012-us-east-1"]
      kms_key_arns = []
    }
  }
  expect_failures = [var.restore_targets]
}

# --- What is actually archived, on a schedule (ADR-0046) ------------------------------

run "the_archive_is_listed_on_the_schedule_the_caller_set" {
  command = apply

  assert {
    condition     = module.recovery.controls.inventory_schedule == var.inventory_schedule
    error_message = "The frequency is the caller's decision, applied unchanged. It is what a scheduled upload of the source cache would have been for, and that upload cannot be scheduled from AWS."
  }
  assert {
    condition     = module.recovery.controls.inventory_versions == "All"
    error_message = "All versions, not only current: the archive is add-only, so a divergence between the two counts is itself the finding."
  }
  assert {
    condition     = module.recovery.controls.inventory_format == "Parquet"
    error_message = "Columnar, for the Athena the platform root already has; CSV would URL-encode every key between the operator and the answer."
  }
  assert {
    condition = length(setintersection(
      toset(module.recovery.controls.inventory_fields),
      toset(["ObjectOwner", "ObjectAcl"]),
    )) == 0
    error_message = "A listing says what is archived and how big it is, not who may read it."
  }
  assert {
    condition     = contains(module.recovery.controls.inventory_fields, "Size") && contains(module.recovery.controls.inventory_fields, "ObjectLockRetainUntilDate")
    error_message = "Enough to answer the drill's completeness check and to show that no version lost its lock."
  }
}

run "the_listing_lands_in_a_bucket_that_can_expire_it" {
  command = apply

  assert {
    condition     = !module.recovery.controls.inventory_locked
    error_message = "A listing is derived and regenerated on the next run, so WORM protects nothing -- and delivery by a service into a default-retention bucket is the unknown ADR-0035 hit with Config and ADR-0042 already carries once for DataSync."
  }
  assert {
    condition     = module.recovery.controls.inventory_versioning == "Enabled" && contains(module.recovery.controls.inventory_deny_sids, "AListingIsNeverPermanentlyDeletedByHand")
    error_message = "The Config bucket's protection instead: versioned, with permanent deletion denied to everyone in the bucket policy."
  }
  assert {
    condition     = module.recovery.controls.inventory_expiry > 0
    error_message = "And an expiry rule, which AWS's own guidance asks for and a locked bucket could not carry."
  }
  assert {
    condition     = length(module.recovery.controls.inventory_allow_source_arns) == 1
    error_message = "S3 writes the report as the service; the source conditions keep another account's inventory configuration from naming this bucket as its destination."
  }
}

run "the_inventory_names_what_the_platform_key_must_admit" {
  command = apply

  assert {
    condition     = tolist(module.recovery.controls.inventory_key_actions) == tolist(["kms:GenerateDataKey"]) && tolist(module.recovery.controls.inventory_key_principals) == tolist(["s3.amazonaws.com"])
    error_message = "Without this on the caller's key S3 cannot encrypt the report and it is never delivered -- silence with a green light. S3 Inventory does not accept the AWS managed aws/s3 key."
  }
  assert {
    condition     = tolist(module.recovery.controls.inventory_key_source_arns) == tolist(["arn:aws:s3:::cascade-recovery-123456789012-us-east-1"])
    error_message = "Scoped to this bucket, and built from names rather than from the key, so using it in that key's own policy is not a cycle."
  }
}

run "the_simulation_is_denied_the_archive_listing_too" {
  command = apply
  variables {
    simulation_principal_arns = ["arn:aws:iam::123456789012:role/cascade-sim"]
  }

  assert {
    condition     = contains(module.recovery.controls.inventory_deny_sids, "SimulationNeverReadsTheArchiveListing")
    error_message = "A key is not a label, but the simulation has no reason to read a listing of the archive it is denied."
  }
}
