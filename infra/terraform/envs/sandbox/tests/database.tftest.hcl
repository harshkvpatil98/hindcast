# The database module, offline. Mock providers: no AWS account, no spend.
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
  name       = "t"
  vpc_id     = "vpc-0123"
  subnet_ids = ["subnet-a", "subnet-b"]
  min_acu    = 8
  max_acu    = 8
}

run "storage_encrypted_iam_auth_and_managed_master_password" {
  command = plan
  module {
    source = "../../modules/database"
  }

  assert {
    condition     = aws_rds_cluster.this.storage_encrypted
    error_message = "Aurora storage must be encrypted."
  }
  assert {
    condition     = aws_rds_cluster.this.iam_database_authentication_enabled
    error_message = "IAM database authentication must be enabled (ADR-0034)."
  }
  assert {
    condition     = aws_rds_cluster.this.manage_master_user_password
    error_message = "The master password must be RDS-managed, never in state."
  }
  assert {
    condition     = contains(aws_rds_cluster.this.enabled_cloudwatch_logs_exports, "postgresql")
    error_message = "PostgreSQL logs must be exported."
  }
  assert {
    condition     = aws_rds_cluster.this.deletion_protection
    error_message = "The module's default is deletion protection on; only an environment may turn it off."
  }
}

run "pgvector_is_pinned_at_080" {
  command = plan
  module {
    source = "../../modules/database"
  }

  assert {
    condition     = aws_rds_cluster.this.engine_version == "16.11"
    error_message = "Default engine must be 16.11 -- the newest 16.x that ships exactly pgvector 0.8.0, matching the local M8 measurements."
  }
  assert {
    condition     = alltrue([for i in aws_rds_cluster_instance.this : !i.auto_minor_version_upgrade])
    error_message = "Automatic minor upgrades would move pgvector under the benchmark."
  }
  assert {
    condition     = alltrue([for i in aws_rds_cluster_instance.this : !i.publicly_accessible])
    error_message = "No instance may be publicly accessible."
  }
}

run "tls_is_required" {
  command = plan
  module {
    source = "../../modules/database"
  }

  assert {
    condition     = anytrue([for p in aws_rds_cluster_parameter_group.this.parameter : p.name == "rds.force_ssl" && p.value == "1"])
    error_message = "rds.force_ssl must be 1: plaintext connections refused."
  }
}

run "capacity_is_what_was_asked" {
  command = plan
  module {
    source = "../../modules/database"
  }

  assert {
    condition     = aws_rds_cluster.this.serverlessv2_scaling_configuration[0].min_capacity == 8 && aws_rds_cluster.this.serverlessv2_scaling_configuration[0].max_capacity == 8
    error_message = "Scaling bounds must be exactly the inputs."
  }
}

run "gate_rejects_an_engine_without_pgvector_08" {
  command = plan
  module {
    source = "../../modules/database"
  }
  variables {
    # 16.6 ships pgvector 0.7.4: halfvec exists, but not the 0.8 the stack pins.
    engine_version = "16.6"
  }
  expect_failures = [var.engine_version]
}

run "gate_rejects_min_above_max" {
  command = plan
  module {
    source = "../../modules/database"
  }
  variables {
    min_acu = 16
    max_acu = 8
  }
  expect_failures = [aws_rds_cluster.this]
}

run "no_clone_unless_asked" {
  command = plan
  module {
    source = "../../modules/database"
  }

  assert {
    condition     = length(aws_rds_cluster.clone) == 0
    error_message = "The experiment clone must be opt-in."
  }
}

run "the_clone_is_copy_on_write" {
  command = plan
  module {
    source = "../../modules/database"
  }
  variables {
    experiment_clone = true
  }

  assert {
    condition     = aws_rds_cluster.clone[0].restore_to_point_in_time[0].restore_type == "copy-on-write"
    error_message = "The partitioning experiment must run on a copy-on-write clone, not a full copy."
  }
}
