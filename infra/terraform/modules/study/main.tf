# The study task (ADR-0042): the bench's container with the two things the
# bench must never have -- permission to call a model, and a cache that
# outlives the task.
#
# A second task definition rather than a wider first one. modules/bench
# promises "the bench never calls a model" and its tests hold it to that; the
# ingest runs on that same definition with a path to the internet. Model
# permissions belong to neither. So the study gets its own task role, its own
# security group and its own definition, and shares what is genuinely the same:
# the image, the database wiring and the log group, taken from modules/bench
# as a value (`container_definition`) so they cannot be restated differently.
#
# Which provider is a required input (ADR-0028): it decides where the study's
# largest line item lands, which IAM service authorizes it, and whether the
# task can stay in the isolated subnets.
#
#   anthropic  api.anthropic.com. An API key in Secrets Manager, no IAM, and no
#              private path: those states run in the egress tier
#              (modules/egress), with that host on its allow-list.
#   aws        Claude Platform on AWS, https://aws-external-anthropic.<region>.api.aws.
#              SigV4 as the task role; IAM service prefix aws-external-anthropic,
#              scoped to the one workspace. Anthropic's documentation states
#              PrivateLink is supported and publishes no endpoint service name
#              this project could find -- so a name, when the operator has one,
#              is an input to modules/network, and without one these states
#              run in the egress tier too.
#   bedrock    https://bedrock-mantle.<region>.api.aws/anthropic. SigV4; action
#              bedrock-mantle:CreateInference; interface endpoint
#              com.amazonaws.<region>.bedrock-mantle (both from the Bedrock
#              user guide). It has NO Message Batches API, so it cannot carry
#              the batched fan-out: the task definition is valid for the
#              unbatched phases (compile, the probe, replay) and the root
#              refuses to wire it to the study chain.

data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

locals {
  partition = data.aws_partition.current.partition
  account   = data.aws_caller_identity.current.account_id

  cache_mount = "/cache/llm"

  provider_environment = {
    anthropic = {}
    aws = {
      CASCADE_PROVIDERS__AWS__REGION       = coalesce(var.model_region, "unset")
      CASCADE_PROVIDERS__AWS__WORKSPACE_ID = coalesce(var.workspace_id, "unset")
    }
    bedrock = {
      CASCADE_PROVIDERS__BEDROCK__REGION = coalesce(var.model_region, "unset")
    }
  }

  # The bench's environment with a few keys replaced. Keyed by name so an
  # override replaces its entry instead of adding a duplicate, whose winner ECS
  # does not document.
  environment = merge(
    { for e in var.container_definition.environment : e.name => e.value },
    {
      CASCADE_LLM__MODE      = "record"
      CASCADE_LLM__PROVIDER  = var.model_provider
      CASCADE_LLM__CACHE_DIR = local.cache_mount
    },
    local.provider_environment[var.model_provider],
  )

  container = merge(var.container_definition, {
    environment = [for name in sort(keys(local.environment)) : { name = name, value = local.environment[name] }]
    mountPoints = concat(var.container_definition.mountPoints, [
      { sourceVolume = "llm-cache", containerPath = local.cache_mount, readOnly = false },
    ])
    # The key is resolved by ECS into the container's environment at start. It
    # is never in this definition, in Terraform state, or on a command line.
    secrets = concat(var.container_definition.secrets, var.model_provider == "anthropic" ? [
      { name = "CASCADE_ANTHROPIC_API_KEY", valueFrom = var.api_key_secret.arn },
    ] : [])
    logConfiguration = merge(var.container_definition.logConfiguration, {
      options = merge(var.container_definition.logConfiguration.options, { awslogs-stream-prefix = "study" })
    })
  })
}

# --- Networking: the bench's reach, plus the cache -----------------------------------------

resource "aws_security_group" "task" {
  #checkov:skip=CKV2_AWS_5:Attached at run time -- Step Functions and the printed run-task command pass it to ecs:RunTask; nothing holds it while no task runs.
  name        = "${var.name}-study-task"
  description = "Study task: egress to AWS endpoints, S3, PostgreSQL and the LLM cache; no ingress"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "endpoints" {
  security_group_id = aws_security_group.task.id
  description       = "HTTPS to interface endpoints, the model's among them when it has one"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.vpc_cidr_block
}

resource "aws_vpc_security_group_egress_rule" "s3" {
  security_group_id = aws_security_group.task.id
  description       = "HTTPS to S3 through the gateway endpoint"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  prefix_list_id    = var.s3_prefix_list_id
}

resource "aws_vpc_security_group_egress_rule" "postgres" {
  security_group_id            = aws_security_group.task.id
  description                  = "PostgreSQL to the cluster"
  ip_protocol                  = "tcp"
  from_port                    = var.db_port
  to_port                      = var.db_port
  referenced_security_group_id = var.db_security_group_id
}

resource "aws_vpc_security_group_egress_rule" "cache" {
  security_group_id            = aws_security_group.task.id
  description                  = "NFS to the LLM cache"
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  referenced_security_group_id = var.cache.security_group_id
}

# --- The task role: a model, the cache, the database -- nothing else ------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-study-task"
  description        = "The study's phases: call the chosen model provider, read and write the LLM cache, log in to PostgreSQL"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "task" {
  # Exactly the routes cascade/llm/client.py calls: messages.create,
  # batches.create, batches.retrieve and batches.results (the last two are one
  # action). Not CancelBatchInference or DeleteBatchInference -- stopping a
  # batch is a person's decision, and a deleted batch is deleted evidence --
  # and none of the other sixty actions the service defines (files, skills,
  # agents, workspaces, console federation).
  dynamic "statement" {
    for_each = var.model_provider == "aws" ? [1] : []
    content {
      sid       = "CallClaudePlatformOnAwsInOneWorkspace"
      actions   = ["aws-external-anthropic:CreateInference", "aws-external-anthropic:CreateBatchInference", "aws-external-anthropic:GetBatchInference"]
      resources = ["arn:${local.partition}:aws-external-anthropic:${var.model_region}:${local.account}:workspace/${var.workspace_id}"]
    }
  }

  # bedrock-mantle:CreateInference is the action AWS's own endpoint-policy
  # example names. Its resource types could NOT be verified -- the Service
  # Authorization Reference page for the prefix did not render -- so the
  # resource is "*", bounded to the one region instead. Narrow it to model
  # ARNs when the reference can be read.
  dynamic "statement" {
    for_each = var.model_provider == "bedrock" ? [1] : []
    content {
      sid       = "CallBedrockMantleInOneRegion"
      actions   = ["bedrock-mantle:CreateInference"]
      resources = ["*"]
      condition {
        test     = "StringEquals"
        variable = "aws:RequestedRegion"
        values   = [var.model_region]
      }
    }
  }

  # Mount and write, through the one access point; the file system's own
  # policy refuses any other way in, and refuses root to everybody.
  statement {
    sid       = "ReadAndWriteTheCacheThroughItsAccessPoint"
    actions   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    resources = [var.cache.file_system_arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [var.cache.access_point_arn]
    }
  }

  statement {
    sid       = "ConnectAsAppRoles"
    actions   = ["rds-db:connect"]
    resources = var.db_user_arns
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "study"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# With the first-party API there is no IAM to scope: the credential is a key.
# ECS resolves it as the EXECUTION role, so that role -- modules/bench's -- is
# given this one secret more, and the task role still cannot read it.
data "aws_iam_policy_document" "api_key" {
  count = var.model_provider == "anthropic" ? 1 : 0

  statement {
    sid       = "ResolveTheModelApiKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.api_key_secret.arn]
  }
  statement {
    sid       = "DecryptItThroughSecretsManager"
    actions   = ["kms:Decrypt"]
    resources = [var.api_key_secret.kms_key_arn]
    condition {
      test     = "StringLike"
      variable = "kms:ViaService"
      values   = ["secretsmanager.*.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "api_key" {
  count  = var.model_provider == "anthropic" ? 1 : 0
  name   = "read-model-api-key"
  role   = var.execution_role_name
  policy = data.aws_iam_policy_document.api_key[0].json
}

# --- The task definition -------------------------------------------------------------------------------

resource "aws_ecs_task_definition" "study" {
  family                   = "${var.name}-study"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  ephemeral_storage {
    size_in_gib = var.ephemeral_storage_gib
  }

  # Checkpoints, reports and temporary files: reproducible, so they may die
  # with the task.
  volume {
    name = "scratch"
  }

  # The recordings: not reproducible without paying again, so they may not.
  volume {
    name = "llm-cache"
    efs_volume_configuration {
      file_system_id = var.cache.file_system_id
      # The file system's policy denies plaintext NFS; without this the mount
      # is refused and the task never starts.
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = var.cache.access_point_id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([local.container])

}
