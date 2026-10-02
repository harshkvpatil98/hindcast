# The platform composition, offline.
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

variables {
  region                       = "us-east-1"
  replica_region               = "us-west-2"
  infrastructure_allowance_usd = 50
  guardduty_min_severity       = 7
  report_lock_retention_days   = 365
  inventory_schedule           = "Weekly"
  # Pinned, so a local terraform.tfvars cannot change what is tested.
  create_audit_trail              = true
  create_budget                   = true
  create_cost_anomaly_detection   = true
  create_service_control_policies = true
  enable_recovery                 = true
  enable_reports                  = true
  enable_object_lock              = true
  enable_guardduty                = true
  enable_config                   = true
  disposable                      = false
}

run "the_platform_composes" {
  command = apply

  assert {
    condition     = output.monthly_limit_usd == output.study_ceiling_usd + 50
    error_message = "The budget limit must be the study ceiling plus the allowance."
  }
  assert {
    condition     = startswith(output.recovery_registry_prefix, "s3://") && endswith(output.recovery_registry_prefix, "/registry/")
    error_message = "The platform must say where registry archives go."
  }
  assert {
    condition     = strcontains(output.events_bucket, "123456789012-us-east-1")
    error_message = "Bucket names carry the account and region."
  }
}

run "the_region_guardrail_always_admits_the_platforms_own_two_regions" {
  command = plan

  assert {
    condition     = toset(one(one(module.guardrails.regions_statement).condition).values) == toset(["us-east-1", "us-west-2"])
    error_message = "A region SCP that omitted the replica region would deny the platform's own recovery bucket."
  }
}

run "guardduty_findings_reach_the_one_alerts_topic_and_the_topic_admits_them" {
  command = apply

  assert {
    condition     = module.audit.controls.findings_target_arn == module.governance.alerts_topic_arn
    error_message = "Findings go where the budget alerts go."
  }
  assert {
    condition     = tolist(module.audit.controls.topic_allow_resources) == tolist([module.governance.alerts_topic_arn])
    error_message = "The audit module's topic statements are written for the governance module's topic."
  }
  assert {
    condition     = module.governance.topic_policy_source_document_count == 2
    error_message = "The topic policy merges the sandbox's statements AND the audit module's: a root that forgot the second drops every finding, with no error."
  }
}

run "the_recovery_output_is_what_the_sandbox_takes" {
  command = apply

  assert {
    condition     = output.recovery.llm_cache_prefix == "llm-cache" && can(regex("^arn:aws:s3:::", output.recovery.bucket_arn)) && output.recovery.kms_key_arn == aws_kms_key.platform.arn
    error_message = "The sandbox's `study.recovery` is this object, passed whole: bucket, key, prefix."
  }
  assert {
    condition     = endswith(output.recovery_source_cache_prefix, "/source-cache/")
    error_message = "The platform must say where the source cache goes."
  }
}

# --- An account that already has a trail, a budget and the services monitor ----------

# The defaults themselves are asserted statically (test_infra_invariants.py):
# this file pins the switches on, so it cannot see them.
run "with_the_switches_on_the_root_makes_the_trail_the_budget_and_the_monitor" {
  command = apply

  assert {
    condition     = output.account_level_services.s3_data_events_kept && output.account_level_services.audit_trail_arn != null && output.account_level_services.budget_name == "cascade-study" && output.account_level_services.anomaly_monitor_arn != null
    error_message = "With the switches on the root makes all three, and reports each."
  }
}

run "an_account_with_its_own_trail_budget_and_monitor_keeps_them" {
  command = apply
  variables {
    create_audit_trail              = false
    create_budget                   = false
    create_cost_anomaly_detection   = false
    create_service_control_policies = false
  }

  assert {
    condition     = output.account_level_services.audit_trail_arn == null && output.account_level_services.budget_name == null && output.account_level_services.anomaly_monitor_arn == null && !output.account_level_services.s3_data_events_kept && !output.account_level_services.service_control_scps
    error_message = "Each switch reaches its module: nothing the account already carries is made a second time."
  }
  assert {
    condition     = output.account_level_services.guardduty_detector != null && output.account_level_services.config_bucket != null
    error_message = "GuardDuty and Config are still this root's."
  }
  assert {
    condition     = module.audit.controls.findings_target_arn == module.governance.alerts_topic_arn && module.governance.topic_policy_source_document_count == 2
    error_message = "Findings still reach the one alerts topic, and the topic still admits them."
  }
}

# --- The demonstration profile (ADR-0054, portfolio.tfvars) ------------------------------

run "with_every_tier_on_the_root_is_the_platform_as_designed" {
  command = apply

  assert {
    condition     = output.deployment == { event_lake = true, lake_object_lock = true, recovery = true, reports = true, guardduty = true, config = true, disposable = false }
    error_message = "The defaults are the whole platform, locked and not disposable."
  }
  assert {
    condition     = output.recovery != null && output.reports_publish != null && output.recovery_restore_role_arn != null
    error_message = "With the tiers on, the outputs the sandbox takes are there."
  }
}

run "the_portfolio_profile_is_the_key_the_lake_and_the_topic" {
  command = apply
  variables {
    create_audit_trail              = false
    create_budget                   = false
    create_cost_anomaly_detection   = false
    create_service_control_policies = false
    enable_recovery                 = false
    enable_reports                  = false
    enable_object_lock              = false
    enable_guardduty                = false
    enable_config                   = false
    disposable                      = true
  }

  assert {
    condition     = output.deployment == { event_lake = true, lake_object_lock = false, recovery = false, reports = false, guardduty = false, config = false, disposable = true }
    error_message = "Every tier switch reaches its module: the profile is the lake alone, unlocked and disposable."
  }
  assert {
    condition     = length(module.recovery) == 0 && length(module.reports) == 0
    error_message = "No recovery module means no replica bucket, no replica-region key, no inventory and no restore role."
  }
  assert {
    condition     = output.recovery == null && output.recovery_registry_prefix == null && output.recovery_source_cache_prefix == null && output.recovery_archive_write_policy_arn == null && output.recovery_restore_role_arn == null && output.recovery_inventory_uri == null && output.reports_publish == null && output.reports_uri == null && output.reports_read_policy_arn == null
    error_message = "A tier that is off is a null at the boundary, never an empty string a caller could mistake for a location."
  }
  assert {
    condition     = output.account_level_services.guardduty_detector == null && output.account_level_services.config_bucket == null && output.account_level_services.audit_trail_arn == null && module.audit.controls.buckets_private == null
    error_message = "With the audit tier off the module makes nothing, and does not report a vacuous 'all buckets private'."
  }
  assert {
    condition     = length(module.audit.controls.topic_allow_source_arns) == 0 && length(module.audit.controls.key_policy_statement_ids) == 0
    error_message = "No finding rule means no grant on the topic for one, and no trail means no grant on the key."
  }
  assert {
    condition     = strcontains(output.events_bucket, "123456789012-us-east-1") && output.athena_workgroup == "cascade" && output.alerts_topic_arn != null && output.lake_roles.writer != null && output.lake_roles.analyst != null
    error_message = "What the profile keeps: the lake, its workgroup, its two roles and the alerts topic."
  }
}

run "a_disposable_platform_with_the_lock_on_is_refused" {
  command = plan
  variables {
    disposable = true
  }
  expect_failures = [var.disposable]
}

run "recovery_can_go_while_the_lock_and_the_audit_tier_stay" {
  command = apply
  variables {
    enable_recovery = false
  }

  assert {
    condition     = output.deployment == { event_lake = true, lake_object_lock = true, recovery = false, reports = true, guardduty = true, config = true, disposable = false }
    error_message = "The switches are independent: turning recovery off turns nothing else off."
  }
  assert {
    condition     = length(module.audit.controls.data_event_prefixes) == 3
    error_message = "The trail then records the lake, the reports bucket and the Config bucket -- the buckets that exist, and no ARN for one that does not."
  }
}

run "the_lock_switch_reaches_both_buckets_and_so_does_disposable" {
  command = apply
  variables {
    enable_object_lock = false
    disposable         = true
  }

  assert {
    condition     = !output.deployment.lake_object_lock && output.deployment.disposable && output.deployment.reports
    error_message = "The lake is unlocked and emptiable, and the reports tier is still on."
  }
  assert {
    condition     = module.reports[0].controls.locked == false && module.reports[0].controls.force_destroy == true && module.reports[0].controls.retention_days == null
    error_message = "One switch for both buckets: a reports bucket left locked would be the one thing a destroy could not remove."
  }
}
