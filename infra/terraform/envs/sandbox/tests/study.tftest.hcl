# The study task, offline (ADR-0042): the bench's container with a model and
# a durable cache, and the least IAM that serves each provider.
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
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock" }
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
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:mock" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/mock" }
  }
  mock_resource "aws_cloudwatch_metric_alarm" {
    defaults = { arn = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:mock" }
  }
  mock_resource "aws_efs_file_system" {
    defaults = { arn = "arn:aws:elasticfilesystem:us-east-1:123456789012:file-system/fs-mock" }
  }
  mock_resource "aws_efs_access_point" {
    defaults = { arn = "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-mock" }
  }
  mock_resource "aws_datasync_location_efs" {
    defaults = { arn = "arn:aws:datasync:us-east-1:123456789012:location/loc-efs" }
  }
  mock_resource "aws_datasync_location_s3" {
    defaults = { arn = "arn:aws:datasync:us-east-1:123456789012:location/loc-s3" }
  }
  mock_resource "aws_datasync_task" {
    defaults = { arn = "arn:aws:datasync:us-east-1:123456789012:task/task-mock" }
  }
  mock_resource "aws_eip" {
    defaults = { public_ip = "203.0.113.10" }
  }
}

mock_provider "random" {}

variables {
  name                 = "t"
  vpc_id               = "vpc-0123"
  vpc_cidr_block       = "10.40.0.0/16"
  s3_prefix_list_id    = "pl-0123"
  db_security_group_id = "sg-db"
  db_port              = 5432
  db_user_arns         = ["arn:aws:rds-db:us-east-1:123456789012:dbuser:cluster-ABC/cascade_eval", "arn:aws:rds-db:us-east-1:123456789012:dbuser:cluster-ABC/cascade_sim"]
  execution_role_arn   = "arn:aws:iam::123456789012:role/t-execution"
  execution_role_name  = "t-execution"
  task_cpu             = 4096
  task_memory          = 16384
  cache = {
    file_system_id    = "fs-0123"
    file_system_arn   = "arn:aws:elasticfilesystem:us-east-1:123456789012:file-system/fs-0123"
    access_point_id   = "fsap-0123"
    access_point_arn  = "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-0123"
    security_group_id = "sg-efs"
  }
  # modules/bench's container, as its output would carry it.
  container_definition = {
    name                   = "cascade"
    image                  = "123456789012.dkr.ecr.us-east-1.amazonaws.com/t:m11"
    essential              = true
    command                = ["cascade", "doctor", "--offline"]
    readonlyRootFilesystem = true
    mountPoints            = [{ sourceVolume = "scratch", containerPath = "/scratch", readOnly = false }]
    environment = [
      { name = "CASCADE_LLM__CACHE_DIR", value = "/scratch/llm-cache" },
      { name = "CASCADE_LLM__MODE", value = "replay" },
      { name = "CASCADE_DATABASE__SSLMODE", value = "verify-full" },
      { name = "HF_HUB_OFFLINE", value = "1" },
      { name = "CASCADE_ENV_FILE", value = "/nonexistent" },
    ]
    secrets = [
      { name = "CASCADE_DB_ADMIN_PASSWORD", valueFrom = "arn:aws:secretsmanager:us-east-1:123456789012:secret:master:password::" },
      { name = "CASCADE_DB_SIM_PASSWORD", valueFrom = "arn:aws:secretsmanager:us-east-1:123456789012:secret:sim:password::" },
      { name = "CASCADE_DB_EVAL_PASSWORD", valueFrom = "arn:aws:secretsmanager:us-east-1:123456789012:secret:eval:password::" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options   = { awslogs-group = "/cascade/t/task", awslogs-region = "us-east-1", awslogs-stream-prefix = "bench" }
    }
  }
  model_provider = "aws"
  model_region   = "us-east-1"
  workspace_id   = "wrkspc_01ABC"
  reports = {
    bucket_arn  = "arn:aws:s3:::cascade-reports-123456789012-us-east-1"
    kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/platform"
    prefix      = "reports"
  }
}

run "the_study_records_through_claude_platform_on_aws_onto_a_durable_cache" {
  command = apply
  module {
    source = "../../modules/study"
  }

  assert {
    condition     = output.controls.environment["CASCADE_LLM__MODE"] == "record" && output.controls.environment["CASCADE_LLM__PROVIDER"] == "aws"
    error_message = "The study task records through the chosen provider; the bench's replay pin is overridden, not restated."
  }
  assert {
    condition     = output.controls.environment["CASCADE_LLM__CACHE_DIR"] == "/cache/llm" && output.controls.mount_points["/cache/llm"] == "llm-cache"
    error_message = "The cache directory is the EFS mount, not scratch."
  }
  assert {
    condition     = tolist(output.controls.efs_volumes) == tolist([{ name = "llm-cache", transit = "ENABLED", iam = "ENABLED" }])
    error_message = "The cache volume is EFS, mounted with TLS and IAM authorization through the access point."
  }
  assert {
    condition     = output.controls.environment["CASCADE_PROVIDERS__AWS__REGION"] == "us-east-1" && output.controls.environment["CASCADE_PROVIDERS__AWS__WORKSPACE_ID"] == "wrkspc_01ABC"
    error_message = "Routing is explicit in the task's environment (ADR-0028)."
  }
  assert {
    condition     = output.controls.read_only_root
    error_message = "The root filesystem stays read-only."
  }
  assert {
    condition     = output.controls.environment["CASCADE_DATABASE__SSLMODE"] == "verify-full" && output.controls.environment["HF_HUB_OFFLINE"] == "1" && output.controls.environment["CASCADE_ENV_FILE"] == "/nonexistent"
    error_message = "The bench's settings survive the override: the study definition starts from the bench's, not from a copy."
  }
  assert {
    condition     = length([for name, value in output.controls.environment : name if strcontains(name, "PASSWORD") || strcontains(name, "API_KEY")]) == 0
    error_message = "No credential in plain environment."
  }
  assert {
    condition     = toset(output.controls.secret_names) == toset(["CASCADE_DB_ADMIN_PASSWORD", "CASCADE_DB_SIM_PASSWORD", "CASCADE_DB_EVAL_PASSWORD"])
    error_message = "With SigV4 there is no API key to inject."
  }
}

run "the_grant_is_three_routes_in_one_workspace_and_no_wildcard" {
  command = apply
  module {
    source = "../../modules/study"
  }

  assert {
    condition     = tolist(output.controls.model_actions) == tolist(["aws-external-anthropic:CreateBatchInference", "aws-external-anthropic:CreateInference", "aws-external-anthropic:GetBatchInference"])
    error_message = "Exactly the routes client.py calls: messages.create, batches.create, batches.retrieve/results. Not cancel, not delete, none of the other sixty."
  }
  assert {
    condition     = tolist(output.controls.model_resources) == tolist(["arn:aws:aws-external-anthropic:us-east-1:123456789012:workspace/wrkspc_01ABC"])
    error_message = "Scoped to the one workspace, by the ARN format the service documents."
  }
  assert {
    condition     = length(output.controls.wildcard_services) == 0
    error_message = "No statement in the task role names \"*\" as its resource for this provider."
  }
  assert {
    condition     = length(aws_iam_role_policy.api_key) == 0
    error_message = "No API-key policy is attached to the execution role for a SigV4 provider."
  }
}

run "the_cache_grant_is_through_the_access_point_and_never_root" {
  command = apply
  module {
    source = "../../modules/study"
  }

  assert {
    condition     = tolist(output.controls.cache_actions) == tolist(["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"])
    error_message = "Mount and write; no ClientRootAccess."
  }
  assert {
    condition     = tolist(output.controls.cache_conditions) == tolist([var.cache.access_point_arn])
    error_message = "Conditioned on the one access point, so the file system's own policy and this grant agree on the single way in."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.cache.referenced_security_group_id == var.cache.security_group_id && aws_vpc_security_group_egress_rule.cache.to_port == 2049
    error_message = "NFS to the cache's security group."
  }
}

run "bedrock_is_one_action_in_one_region" {
  command = apply
  module {
    source = "../../modules/study"
  }
  variables {
    model_provider = "bedrock"
    workspace_id   = null
  }

  assert {
    condition     = tolist(output.controls.model_actions) == tolist(["bedrock-mantle:CreateInference"])
    error_message = "The one action the Bedrock user guide's endpoint-policy example names."
  }
  assert {
    condition     = tolist(output.controls.wildcard_services) == tolist(["bedrock-mantle"])
    error_message = "Its resource types could not be verified, so the resource is \"*\" -- and that is the ONLY statement allowed to say so."
  }
  assert {
    condition = anytrue(flatten([
      for s in data.aws_iam_policy_document.task.statement : [
        for c in s.condition : c.variable == "aws:RequestedRegion" && tolist(c.values) == tolist(["us-east-1"])
      ] if s.sid == "CallBedrockMantleInOneRegion"
    ]))
    error_message = "Bounded to the one region instead."
  }
  assert {
    condition     = output.controls.environment["CASCADE_PROVIDERS__BEDROCK__REGION"] == "us-east-1" && output.controls.environment["CASCADE_LLM__PROVIDER"] == "bedrock"
    error_message = "Routing is explicit."
  }
}

run "anthropic_is_a_secret_on_the_execution_role_and_no_iam_grant" {
  command = apply
  module {
    source = "../../modules/study"
  }
  variables {
    model_provider = "anthropic"
    model_region   = null
    workspace_id   = null
    api_key_secret = {
      arn         = "arn:aws:secretsmanager:us-east-1:123456789012:secret:anthropic-api-key"
      kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/secrets"
    }
  }

  assert {
    condition     = contains(output.controls.secret_names, "CASCADE_ANTHROPIC_API_KEY")
    error_message = "The key arrives as an ECS secret."
  }
  assert {
    condition     = length([for s in jsondecode(aws_ecs_task_definition.study.container_definitions)[0].secrets : s if s.name == "CASCADE_ANTHROPIC_API_KEY" && s.valueFrom == var.api_key_secret.arn]) == 1
    error_message = "From the secret the caller named."
  }
  assert {
    condition     = length(output.controls.model_actions) == 0 && length(output.controls.wildcard_services) == 0
    error_message = "There is no IAM service to grant; the credential is the key."
  }
  assert {
    condition     = length(aws_iam_role_policy.api_key) == 1 && aws_iam_role_policy.api_key[0].role == var.execution_role_name
    error_message = "ECS resolves the secret as the execution role, so that is where the one extra read is granted -- the task role still cannot read it."
  }
}

run "claude_code_is_not_offered_here" {
  command = plan
  module {
    source = "../../modules/study"
  }
  variables {
    model_provider = "claude_code"
  }
  expect_failures = [var.model_provider]
}

run "aws_without_a_workspace_is_refused" {
  command = plan
  module {
    source = "../../modules/study"
  }
  variables {
    workspace_id = null
  }
  expect_failures = [var.workspace_id]
}

run "aws_without_a_region_is_refused" {
  command = plan
  module {
    source = "../../modules/study"
  }
  variables {
    model_region = null
  }
  expect_failures = [var.model_region]
}

run "anthropic_without_a_secret_is_refused" {
  command = plan
  module {
    source = "../../modules/study"
  }
  variables {
    model_provider = "anthropic"
    api_key_secret = null
  }
  expect_failures = [var.api_key_secret]
}

# --- Publishing the deliverable (ADR-0046) ---------------------------------------------

run "the_study_task_publishes_reports_and_can_never_read_one_back" {
  command = apply
  module {
    source = "../../modules/study"
  }

  assert {
    condition     = tolist(output.controls.reports_resources) == tolist(["arn:aws:s3:::cascade-reports-123456789012-us-east-1/reports/t/*"])
    error_message = "Under this sandbox's own directory of the reports prefix; two sandboxes must not interleave."
  }
  assert {
    condition = length(setintersection(
      toset(output.controls.s3_actions),
      toset(["s3:GetObject", "s3:GetObjectVersion", "s3:DeleteObject", "s3:DeleteObjectVersion"]),
    )) == 0
    error_message = "A report carries the labels: the process that writes one may not read one back, and may not remove one. Write-only is the design, not an accident of scoping."
  }
  assert {
    condition     = contains(output.controls.reports_actions, "s3:PutObject") && contains(output.controls.reports_actions, "s3:ListBucket")
    error_message = "It can add a report, and list the destination so `aws s3 cp --recursive` knows where it is going."
  }
  assert {
    condition     = output.reports_uri == "s3://cascade-reports-123456789012-us-east-1/reports/t/"
    error_message = "The task is told where to publish by an output, not by an environment variable: a CASCADE_-prefixed name the settings do not define is a validation error by design."
  }
}

run "a_reports_bucket_arn_with_a_path_is_refused" {
  command = plan
  module {
    source = "../../modules/study"
  }
  variables {
    reports = {
      bucket_arn  = "arn:aws:s3:::cascade-reports/reports"
      kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/platform"
      prefix      = "reports"
    }
  }
  expect_failures = [var.reports]
}
