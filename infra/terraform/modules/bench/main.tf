# The bench runner: a Fargate task inside the VPC. Retrieval latency measured
# from a laptop over the internet would measure the internet; measured from a
# task in the same region and VPC as the cluster, it measures Chronofence.
# Fargate rather than an instance: no host to patch, no SSH, nothing running
# between measurements.

data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  partition = data.aws_partition.current.partition
  region    = data.aws_region.current.region
  account   = data.aws_caller_identity.current.account_id
  # The Postgres roles the task may log in as with an IAM token.
  db_users = [for role in sort(keys(var.role_secret_arns)) : "arn:${local.partition}:rds-db:${local.region}:${local.account}:dbuser:${var.db_cluster_resource_id}/${role}"]
}

# --- Artifacts bucket (the corpus dump) ------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  #checkov:skip=CKV_AWS_18:Server access logs for a sandbox artifact bucket are deferred to M12, where CloudTrail data events are the audit trail for all buckets.
  #checkov:skip=CKV_AWS_144:A corpus dump is reproducible from the pipeline; cross-region replication would pay to copy it twice.
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events from this bucket.
  bucket        = var.artifacts_bucket_name
  force_destroy = var.disposable
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    id     = "tidy"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

data "aws_iam_policy_document" "artifacts" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.artifacts.json
}

# --- Image registry --------------------------------------------------------------

resource "aws_ecr_repository" "bench" {
  name         = var.name
  force_delete = var.disposable
  # Immutable: a tag names one image forever, so a published bench result can
  # always be traced to the exact code that produced it.
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }
}

resource "aws_ecr_lifecycle_policy" "bench" {
  repository = aws_ecr_repository.bench.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

# --- Task networking ---------------------------------------------------------------

resource "aws_security_group" "task" {
  #checkov:skip=CKV2_AWS_5:Attached at run time -- the run_task output passes it to aws ecs run-task.
  name        = "${var.name}-task"
  description = "Bench task: egress to AWS endpoints, S3 and PostgreSQL only; no ingress"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_egress_rule" "endpoints" {
  security_group_id = aws_security_group.task.id
  description       = "HTTPS to interface endpoints"
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

# --- Roles -----------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
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

# Execution role: what ECS itself needs to start the task -- pull the image,
# write logs, and resolve the three secrets into environment variables.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    sid       = "ReadTheseSecretsOnly"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = concat([var.master_user_secret_arn], [for role in sort(keys(var.role_secret_arns)) : var.role_secret_arns[role]])
  }
  statement {
    sid       = "DecryptThem"
    actions   = ["kms:Decrypt"]
    resources = [var.db_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-db-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# Task role: what the bench code itself may do.
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid       = "ReadCorpusDump"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"]
  }
  statement {
    sid       = "DecryptArtifacts"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
  # IAM database authentication, for the move off stored passwords (ADR-0034).
  statement {
    sid       = "ConnectAsAppRoles"
    actions   = ["rds-db:connect"]
    resources = local.db_users
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "bench"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- Cluster and task -----------------------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "task" {
  #checkov:skip=CKV_AWS_338:Sandbox retention, deliberately short: the environment is created, measured and destroyed. M12's production design sets 365 days.
  name              = "/cascade/${var.name}/task"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

resource "aws_ecs_task_definition" "bench" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  ephemeral_storage {
    size_in_gib = var.ephemeral_storage_gib
  }

  # The container's only writable location. The root filesystem is read-only,
  # so the image cannot be modified at run time; everything the CLI writes is
  # redirected here through settings (below).
  volume {
    name = "scratch"
  }

  container_definitions = jsonencode([{
    name      = "cascade"
    image     = "${aws_ecr_repository.bench.repository_url}:${var.image_tag}"
    essential = true
    # Overridden per run; this default proves the wiring without touching data.
    command                = ["cascade", "doctor", "--offline"]
    readonlyRootFilesystem = true
    mountPoints            = [{ sourceVolume = "scratch", containerPath = "/scratch", readOnly = false }]
    environment = [
      # Every path the CLI writes, moved off the read-only root.
      { name = "CASCADE_LLM__CACHE_DIR", value = "/scratch/llm-cache" },
      { name = "CASCADE_PATHS__CHECKPOINTS", value = "/scratch/checkpoints" },
      { name = "CASCADE_PATHS__REPORTS", value = "/scratch/reports" },
      { name = "CASCADE_PATHS__GRAPHS", value = "/scratch/graphs" },
      { name = "TMPDIR", value = "/scratch/tmp" },
      # The VPC has no internet: the embedding model is baked into the image
      # and the hub is never consulted (M7 found the loader otherwise calls it
      # even when the weights are cached).
      { name = "HF_HUB_OFFLINE", value = "1" },
      { name = "TRANSFORMERS_OFFLINE", value = "1" },
      { name = "CASCADE_DATABASE__HOST", value = var.db_endpoint },
      { name = "CASCADE_DATABASE__PORT", value = tostring(var.db_port) },
      { name = "CASCADE_DATABASE__NAME", value = var.db_name },
      # The server certificate is verified against the RDS bundle baked into
      # the image; the cluster refuses plaintext from its side (rds.force_ssl).
      { name = "CASCADE_DATABASE__SSLMODE", value = "verify-full" },
      { name = "CASCADE_DATABASE__SSLROOTCERT", value = "/etc/ssl/rds/global-bundle.pem" },
      { name = "CASCADE_DATABASE__AUTH", value = var.db_auth },
      { name = "CASCADE_DATABASE__IAM_REGION", value = local.region },
      # No .env inside the image: every value arrives from this definition.
      { name = "CASCADE_ENV_FILE", value = "/nonexistent" },
      # The bench and the migrations never call a model.
      { name = "CASCADE_LLM__MODE", value = "replay" },
    ]
    secrets = [
      { name = "CASCADE_DB_ADMIN_PASSWORD", valueFrom = "${var.master_user_secret_arn}:password::" },
      { name = "CASCADE_DB_SIM_PASSWORD", valueFrom = "${var.role_secret_arns["cascade_sim"]}:password::" },
      { name = "CASCADE_DB_EVAL_PASSWORD", valueFrom = "${var.role_secret_arns["cascade_eval"]}:password::" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.task.name
        awslogs-region        = local.region
        awslogs-stream-prefix = "bench"
      }
    }
  }])
}
