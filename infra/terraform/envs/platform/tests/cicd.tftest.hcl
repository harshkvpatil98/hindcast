# Deployment identity, offline: who may plan, who may apply, and from where.
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
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
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
  mock_resource "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}

variables {
  name              = "t"
  github_repository = "utkarsh430/cascade"
  apply_policy_arns = ["arn:aws:iam::aws:policy/PowerUserAccess"]
  state_bucket_arn  = "arn:aws:s3:::cascade-tfstate-123456789012-us-east-1"
  state_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/mock"
}

run "plan_is_for_pull_requests_and_apply_is_for_the_reviewed_environment" {
  command = plan
  module {
    source = "../../modules/cicd"
  }

  assert {
    condition     = output.controls.plan_subjects == ["repo:utkarsh430/cascade:pull_request"]
    error_message = "The plan role trusts pull requests in this repository, and nothing else."
  }
  assert {
    condition     = output.controls.apply_subjects == ["repo:utkarsh430/cascade:environment:production"]
    error_message = "The apply role trusts the deployment environment -- not a branch, which needs no reviewer."
  }
}

run "subjects_are_matched_whole_and_the_audience_is_sts" {
  command = plan
  module {
    source = "../../modules/cicd"
  }

  assert {
    condition     = output.controls.subject_tests == tolist(["StringEquals"])
    error_message = "StringLike on the subject is how a lookalike repository gets in."
  }
  assert {
    condition     = output.controls.audiences == tolist(["sts.amazonaws.com"])
    error_message = "The token must have been minted for AWS STS."
  }
  assert {
    condition     = alltrue([for s in concat(output.controls.plan_subjects, output.controls.apply_subjects) : !strcontains(s, "*")])
    error_message = "No wildcard in any trusted subject."
  }
}

run "plan_can_only_read" {
  command = plan
  module {
    source = "../../modules/cicd"
  }

  assert {
    condition     = output.controls.plan_policies == ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
    error_message = "The plan role is read-only."
  }
  assert {
    condition = length([
      for s in data.aws_iam_policy_document.state_read.statement : s
      if length(setintersection(s.actions, ["s3:PutObject", "s3:DeleteObject"])) > 0
    ]) == 0
    error_message = "A plan must not be able to write or delete state."
  }
}

run "apply_carries_exactly_the_policies_somebody_chose" {
  command = plan
  module {
    source = "../../modules/cicd"
  }

  assert {
    condition     = output.controls.apply_policies == tolist(["arn:aws:iam::aws:policy/PowerUserAccess"])
    error_message = "The apply role's permissions are the caller's decision, in full."
  }
}

run "a_wildcard_repository_is_refused" {
  command = plan
  module {
    source = "../../modules/cicd"
  }
  variables {
    github_repository = "utkarsh430/*"
  }
  expect_failures = [var.github_repository]
}

run "apply_permissions_cannot_be_left_to_a_default" {
  command = plan
  module {
    source = "../../modules/cicd"
  }
  variables {
    apply_policy_arns = []
  }
  expect_failures = [var.apply_policy_arns]
}
