# Cost governance, offline.
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

variables {
  name                         = "t"
  study_config_path            = "../../../../configs/base.yaml"
  infrastructure_allowance_usd = 50
  kms_key_arn                  = "arn:aws:kms:us-east-1:123456789012:key/mock"
}

run "the_budget_is_derived_from_the_study_configuration" {
  command = plan
  module {
    source = "../../modules/governance"
  }

  assert {
    condition     = output.study_ceiling_usd == sum(values(output.phase_ceilings_usd)) && output.study_ceiling_usd > 0
    error_message = "The study ceiling must be the sum of the phase ceilings read from base.yaml."
  }
  assert {
    condition     = contains(keys(output.phase_ceilings_usd), "simulate") && contains(keys(output.phase_ceilings_usd), "compile")
    error_message = "The phases must come from the study configuration, not be restated."
  }
  assert {
    condition     = aws_budgets_budget.study.limit_amount == format("%.2f", output.study_ceiling_usd + 50)
    error_message = "Monthly limit = study ceiling + the infrastructure allowance."
  }
}

run "the_budget_follows_a_different_configuration" {
  command = plan
  module {
    source = "../../modules/governance"
  }
  variables {
    study_config_path = "tests/fixtures/study.yaml"
  }

  assert {
    condition     = output.study_ceiling_usd == 4
    error_message = "With ceilings of 1.5 and 2.5 the study ceiling is 4: it must be read, not restated."
  }
  assert {
    condition     = aws_budgets_budget.study.limit_amount == "54.00"
    error_message = "4 + the 50 allowance."
  }
}

run "alerts_fire_before_the_limit_and_on_forecast" {
  command = plan
  module {
    source = "../../modules/governance"
  }

  assert {
    condition     = toset([for n in aws_budgets_budget.study.notification : "${n.notification_type}:${n.threshold}"]) == toset(["ACTUAL:50", "ACTUAL:80", "ACTUAL:100", "FORECASTED:100"])
    error_message = "Alerts at 50/80/100% actual and 100% forecast."
  }
  assert {
    condition     = aws_sns_topic.alerts.kms_master_key_id != null
    error_message = "The alerts topic must be encrypted."
  }
}

run "an_invented_allowance_is_not_accepted" {
  command = plan
  module {
    source = "../../modules/governance"
  }
  variables {
    infrastructure_allowance_usd = 0
  }
  expect_failures = [var.infrastructure_allowance_usd]
}
