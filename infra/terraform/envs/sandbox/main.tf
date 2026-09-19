# M11 sandbox: an isolated VPC, Aurora PostgreSQL 16 + pgvector 0.8.0, and a
# Fargate bench runner beside it. Built to be created, measured and destroyed:
# the corpus dump in S3 makes the database reproducible, so deletion
# protection is off here and on by default in the module.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account          = data.aws_caller_identity.current.account_id
  partition        = data.aws_partition.current.partition
  artifacts_bucket = "${var.name}-artifacts-${local.account}-${var.region}"
}

# --- Platform key: logs, ECR, artifacts -----------------------------------------

data "aws_iam_policy_document" "platform_key" {
  #checkov:skip=CKV_AWS_111:A KMS key policy's "kms:*" for the account root is AWS's default key policy: it delegates to IAM, and Resource "*" in a key policy means this key only.
  #checkov:skip=CKV_AWS_356:Resource "*" in a key policy refers to the key itself, not to all resources.
  #checkov:skip=CKV_AWS_109:As above -- the account-root statement is the standard delegation to IAM.
  statement {
    sid       = "AccountAdministers"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account}:root"]
    }
  }

  statement {
    sid       = "CloudWatchLogsForCascadeGroups"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${var.region}:${local.account}:log-group:/cascade/*"]
    }
  }
}

resource "aws_kms_key" "platform" {
  description             = "${var.name}: flow logs, task logs, ECR images, artifacts"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.platform_key.json
}

resource "aws_kms_alias" "platform" {
  name          = "alias/${var.name}-platform"
  target_key_id = aws_kms_key.platform.key_id
}

# --- Modules -----------------------------------------------------------------------

module "network" {
  source                 = "../../modules/network"
  name                   = var.name
  availability_zones     = var.availability_zones
  log_kms_key_arn        = aws_kms_key.platform.arn
  s3_allowed_bucket_arns = ["arn:${local.partition}:s3:::${local.artifacts_bucket}"]
}

module "database" {
  source         = "../../modules/database"
  name           = var.name
  vpc_id         = module.network.vpc_id
  subnet_ids     = module.network.private_subnet_ids
  engine_version = var.engine_version
  # Fixed capacity: see var.acu.
  min_acu                   = var.acu
  max_acu                   = var.acu
  client_security_group_ids = { bench = module.bench.security_group_id }
  # A sandbox is rebuilt from the corpus dump, not recovered from a snapshot.
  deletion_protection = false
  skip_final_snapshot = true
  experiment_clone    = var.experiment_clone
}

module "pipeline" {
  count  = var.pipeline == null ? 0 : 1
  source = "../../modules/pipeline"
  name   = var.name

  cluster_arn             = module.bench.cluster_arn
  task_definition_arn     = module.bench.task_definition_arn
  task_execution_role_arn = module.bench.execution_role_arn
  task_role_arn           = module.bench.task_role_arn
  subnet_ids              = module.network.private_subnet_ids
  security_group_ids      = [module.bench.security_group_id]
  alerts_topic_arn        = var.pipeline.alerts_topic_arn
  fanout_timeout_seconds  = var.pipeline.fanout_timeout_seconds
  # This root's key already admits CloudWatch Logs for /cascade/* groups.
  kms_key_arn        = aws_kms_key.platform.arn
  log_retention_days = 30
}

module "observability" {
  count  = var.observability == null ? 0 : 1
  source = "../../modules/observability"
  name   = var.name

  alerts_topic_arn          = var.observability.alerts_topic_arn
  freeable_memory_low_bytes = var.observability.freeable_memory_low_bytes
  database_connections_high = var.observability.database_connections_high

  cluster_arn           = module.bench.cluster_arn
  db_cluster_identifier = module.database.cluster_identifier
  task_log_group_name   = module.bench.log_group
  # Capacity is pinned for measurement (see var.acu), which the module detects:
  # with min == max the capacity alarms would fire for the cluster's whole
  # life, so it watches CPU against that capacity instead.
  min_acu = var.acu
  max_acu = var.acu

  # Failed, timed-out and aborted executions alert too, when the chains exist.
  state_machine_arns = var.pipeline == null ? [] : [module.pipeline[0].ingest_state_machine_arn, module.pipeline[0].study_state_machine_arn]
}

module "bench" {
  source                 = "../../modules/bench"
  name                   = var.name
  vpc_id                 = module.network.vpc_id
  vpc_cidr_block         = module.network.vpc_cidr_block
  s3_prefix_list_id      = module.network.s3_prefix_list_id
  kms_key_arn            = aws_kms_key.platform.arn
  db_kms_key_arn         = module.database.kms_key_arn
  db_security_group_id   = module.database.security_group_id
  db_endpoint            = module.database.endpoint
  db_port                = module.database.port
  db_name                = "cascade"
  db_cluster_resource_id = module.database.cluster_resource_id
  master_user_secret_arn = module.database.master_user_secret_arn
  role_secret_arns       = module.database.role_secret_arns
  artifacts_bucket_name  = local.artifacts_bucket
  image_tag              = var.image_tag
  # Created, measured, destroyed: see the module's `disposable`.
  disposable = true
}
