# The network module, offline.
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
}

mock_provider "random" {}

variables {
  name               = "t"
  availability_zones = ["us-east-1a", "us-east-1b"]
  log_kms_key_arn    = "arn:aws:kms:us-east-1:123456789012:key/test"
}

run "subnets_are_private" {
  command = plan
  module {
    source = "../../modules/network"
  }

  assert {
    condition     = alltrue([for s in aws_subnet.private : !s.map_public_ip_on_launch])
    error_message = "No subnet may assign public IPs."
  }
  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "One private subnet per AZ."
  }
}

run "endpoints_are_the_minimal_set" {
  command = plan
  module {
    source = "../../modules/network"
  }

  assert {
    condition     = toset(keys(aws_vpc_endpoint.interface)) == toset(["ecr.api", "ecr.dkr", "logs", "secretsmanager"])
    error_message = "Interface endpoints are billed per AZ-hour; the default set is what the bench task needs and nothing more."
  }
  assert {
    condition     = aws_vpc_endpoint.s3.vpc_endpoint_type == "Gateway"
    error_message = "S3 is reached through the (free) gateway endpoint."
  }
  assert {
    condition     = alltrue([for e in aws_vpc_endpoint.interface : e.private_dns_enabled])
    error_message = "Private DNS makes the SDK's default endpoints resolve to the VPC."
  }
}

run "flow_logs_capture_everything" {
  command = plan
  module {
    source = "../../modules/network"
  }

  assert {
    condition     = aws_flow_log.this.traffic_type == "ALL"
    error_message = "Flow logs must record accepted and rejected traffic."
  }
}

run "one_az_is_refused" {
  command = plan
  module {
    source = "../../modules/network"
  }
  variables {
    availability_zones = ["us-east-1a"]
  }
  expect_failures = [var.availability_zones]
}
