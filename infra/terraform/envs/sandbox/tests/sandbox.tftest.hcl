# The sandbox composition, offline.
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
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:mock" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/mock" }
  }
  mock_resource "aws_cloudwatch_metric_alarm" {
    defaults = { arn = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }
}

mock_provider "random" {}

variables {
  region             = "us-east-1"
  availability_zones = ["us-east-1a", "us-east-1b"]
}

run "the_stack_composes_and_runs_tasks_without_public_ips" {
  command = apply

  assert {
    condition     = strcontains(output.run_task, "assignPublicIp=DISABLED")
    error_message = "Bench tasks must never get a public IP."
  }
  assert {
    condition     = strcontains(output.run_task, "--launch-type FARGATE")
    error_message = "The printed command must launch on Fargate."
  }
}

run "a_malformed_region_is_refused" {
  command = plan
  variables {
    region = "not-a-region"
  }
  expect_failures = [var.region]
}

run "observability_is_off_until_a_topic_and_thresholds_are_given" {
  command = plan

  assert {
    condition     = length(module.observability) == 0
    error_message = "With no observability input the sandbox must create no alarms -- and need no thresholds."
  }
}

run "observability_watches_this_sandbox_with_the_thresholds_given" {
  command = plan
  variables {
    observability = {
      alerts_topic_arn          = "arn:aws:sns:us-east-1:123456789012:cascade-cost-alerts"
      freeable_memory_low_bytes = 123456789
      database_connections_high = 37
    }
  }

  assert {
    condition     = length(module.observability) == 1
    error_message = "Given a topic and thresholds, the sandbox is watched."
  }
}

run "the_chains_exist_only_when_asked_for_and_feed_the_failure_alarms" {
  command = plan
  variables {
    pipeline = {
      alerts_topic_arn       = "arn:aws:sns:us-east-1:123456789012:cascade-cost-alerts"
      fanout_timeout_seconds = 86400
    }
    observability = {
      alerts_topic_arn          = "arn:aws:sns:us-east-1:123456789012:cascade-cost-alerts"
      freeable_memory_low_bytes = 123456789
      database_connections_high = 37
    }
  }

  assert {
    condition     = length(module.pipeline) == 1
    error_message = "Given a topic and a fan-out timeout, the two chains are created."
  }
}

run "no_chains_by_default" {
  command = plan

  assert {
    condition     = length(module.pipeline) == 0
    error_message = "A plain sandbox creates no state machines."
  }
}
