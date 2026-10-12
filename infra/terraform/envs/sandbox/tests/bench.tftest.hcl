# The bench module, offline. Applied against mocks so the rendered task definition is concrete.
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
  name                   = "t"
  vpc_id                 = "vpc-0123"
  vpc_cidr_block         = "10.40.0.0/16"
  s3_prefix_list_id      = "pl-0123"
  kms_key_arn            = "arn:aws:kms:us-east-1:123456789012:key/platform"
  db_kms_key_arn         = "arn:aws:kms:us-east-1:123456789012:key/data"
  db_security_group_id   = "sg-db"
  db_endpoint            = "t.cluster-x.us-east-1.rds.amazonaws.com"
  db_port                = 5432
  db_name                = "cascade"
  db_cluster_resource_id = "cluster-ABC"
  master_user_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:master"
  role_secret_arns = {
    cascade_sim  = "arn:aws:secretsmanager:us-east-1:123456789012:secret:sim"
    cascade_eval = "arn:aws:secretsmanager:us-east-1:123456789012:secret:eval"
  }
  artifacts_bucket_name = "t-artifacts"
}

run "images_are_immutable_and_scanned" {
  command = plan
  module {
    source = "../../modules/bench"
  }

  assert {
    condition     = aws_ecr_repository.bench.image_tag_mutability == "IMMUTABLE"
    error_message = "A tag must name one image forever, so a result traces to its code."
  }
  assert {
    condition     = aws_ecr_repository.bench.image_scanning_configuration[0].scan_on_push
    error_message = "Images must be scanned on push."
  }
}

run "the_artifacts_bucket_is_private_and_encrypted" {
  command = plan
  module {
    source = "../../modules/bench"
  }

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.artifacts.block_public_acls,
      aws_s3_bucket_public_access_block.artifacts.block_public_policy,
      aws_s3_bucket_public_access_block.artifacts.ignore_public_acls,
      aws_s3_bucket_public_access_block.artifacts.restrict_public_buckets,
    ])
    error_message = "Every public-access block must be on."
  }
  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.artifacts.rule).apply_server_side_encryption_by_default).sse_algorithm == "aws:kms"
    error_message = "Artifacts must be encrypted with the platform CMK."
  }
}

run "the_task_is_private_fargate_with_secrets_injected" {
  command = apply
  module {
    source = "../../modules/bench"
  }

  assert {
    condition     = aws_ecs_task_definition.bench.network_mode == "awsvpc" && contains(aws_ecs_task_definition.bench.requires_compatibilities, "FARGATE")
    error_message = "The bench runs as a Fargate task in the VPC."
  }
  assert {
    condition     = toset([for s in jsondecode(aws_ecs_task_definition.bench.container_definitions)[0].secrets : s.name]) == toset(["CASCADE_DB_ADMIN_PASSWORD", "CASCADE_DB_SIM_PASSWORD", "CASCADE_DB_EVAL_PASSWORD"])
    error_message = "Database passwords arrive as ECS secrets and nowhere else."
  }
  assert {
    condition     = length([for e in jsondecode(aws_ecs_task_definition.bench.container_definitions)[0].environment : e if strcontains(e.name, "PASSWORD")]) == 0
    error_message = "No password may appear in plain environment variables."
  }
  assert {
    condition     = contains([for e in jsondecode(aws_ecs_task_definition.bench.container_definitions)[0].environment : "${e.name}=${e.value}"], "CASCADE_LLM__MODE=replay")
    error_message = "The bench task never calls a model."
  }
  assert {
    condition     = contains([for e in jsondecode(aws_ecs_task_definition.bench.container_definitions)[0].environment : "${e.name}=${e.value}"], "CASCADE_DATABASE__SSLMODE=verify-full")
    error_message = "The task must verify the database's certificate, not merely encrypt."
  }
  assert {
    condition     = jsondecode(aws_ecs_task_definition.bench.container_definitions)[0].readonlyRootFilesystem
    error_message = "The container's root filesystem must be read-only."
  }
  assert {
    condition     = contains([for e in jsondecode(aws_ecs_task_definition.bench.container_definitions)[0].environment : "${e.name}=${e.value}"], "HF_HUB_OFFLINE=1")
    error_message = "The VPC has no internet: the model must load from the image, never the hub."
  }
}
