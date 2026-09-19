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

  # How the study's tasks reach their model provider (ADR-0042). A provider
  # with a PrivateLink service gets an interface endpoint and the tasks never
  # leave the isolated subnets; one without is a host on the egress tier's
  # allow-list, and only the states that call a model are moved there.
  model_provider = var.study == null ? "none" : var.study.model_provider
  model_endpoint = var.study == null ? null : var.study.model_endpoint_service_name

  # Bedrock: com.amazonaws.<region>.bedrock-mantle, from the Bedrock user guide.
  model_interface_endpoints = local.model_provider == "bedrock" ? ["bedrock-mantle"] : []
  # Claude Platform on AWS: only when the operator supplies the service name.
  model_named_endpoints = local.model_provider == "aws" && local.model_endpoint != null ? { "claude-platform" = local.model_endpoint } : {}

  # Host names from cascade/llm/providers.py -- reproduced, not invented.
  model_hosts = (
    local.model_provider == "anthropic" ? ["api.anthropic.com"] :
    local.model_provider == "aws" && local.model_endpoint == null ? ["aws-external-anthropic.${var.study.model_region}.api.aws"] :
    []
  )
  model_calls_use_egress = length(local.model_hosts) > 0
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
  source                    = "../../modules/network"
  name                      = var.name
  availability_zones        = var.availability_zones
  log_kms_key_arn           = aws_kms_key.platform.arn
  s3_allowed_bucket_arns    = ["arn:${local.partition}:s3:::${local.artifacts_bucket}"]
  extra_interface_endpoints = local.model_interface_endpoints
  named_interface_endpoints = local.model_named_endpoints
}

# The way out, for the ingest -- and for the study's model calls when the
# provider has no private path. Off unless asked for: the default sandbox is
# ADR-0034's isolated VPC, and nothing below exists in it.
module "egress" {
  count  = var.egress == null ? 0 : 1
  source = "../../modules/egress"
  name   = var.name
  vpc_id = module.network.vpc_id

  availability_zone = var.egress.availability_zone
  # The top two /24s of the VPC. modules/network carves /20s from the bottom,
  # one per AZ, and would need sixteen AZs to reach these.
  public_subnet_cidr     = cidrsubnet(module.network.vpc_cidr_block, 8, 255)
  workload_subnet_cidr   = cidrsubnet(module.network.vpc_cidr_block, 8, 254)
  s3_gateway_endpoint_id = module.network.s3_endpoint_id

  # The operator's list, plus the model's host when it is reached this way.
  allowed_domains     = distinct(concat(var.egress.allowed_domains, local.model_hosts))
  dns_firewall_action = var.egress.dns_firewall_action
  log_kms_key_arn     = aws_kms_key.platform.arn
}

module "database" {
  source         = "../../modules/database"
  name           = var.name
  vpc_id         = module.network.vpc_id
  subnet_ids     = module.network.private_subnet_ids
  engine_version = var.engine_version
  # Fixed capacity: see var.acu.
  min_acu = var.acu
  max_acu = var.acu
  client_security_group_ids = merge(
    { bench = module.bench.security_group_id },
    var.study == null ? {} : { study = module.study[0].security_group_id },
  )
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

  # Without these two the chains exist and cannot finish: see the module.
  egress_network = var.egress == null ? null : {
    subnet_ids         = module.egress[0].subnet_ids
    security_group_ids = [module.egress[0].security_group_id]
  }
  study_task = var.study == null ? null : {
    task_definition_arn     = module.study[0].task_definition_arn
    task_execution_role_arn = module.bench.execution_role_arn
    task_role_arn           = module.study[0].task_role_arn
    security_group_ids      = [module.study[0].security_group_id]
  }
  model_calls_use_egress = local.model_calls_use_egress
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

# --- The study: a model, and a cache that outlives the task (ADR-0042) -------------------

module "cache" {
  count  = var.study == null ? 0 : 1
  source = "../../modules/cache"
  name   = var.name
  vpc_id = module.network.vpc_id
  # Mount targets in the isolated tier only. A task in the egress tier's subnet
  # reaches the one in its own AZ over the VPC's local route.
  subnet_ids                = module.network.private_subnet_ids
  kms_key_arn               = aws_kms_key.platform.arn
  client_security_group_ids = { study = module.study[0].security_group_id }

  recovery_bucket_arn      = var.study.recovery.bucket_arn
  recovery_kms_key_arn     = var.study.recovery.kms_key_arn
  recovery_prefix          = var.study.recovery.llm_cache_prefix
  sync_schedule_expression = var.study.sync_schedule_expression
  alerts_topic_arn         = var.observability == null ? null : var.observability.alerts_topic_arn
}

module "study" {
  count  = var.study == null ? 0 : 1
  source = "../../modules/study"
  name   = var.name

  vpc_id               = module.network.vpc_id
  vpc_cidr_block       = module.network.vpc_cidr_block
  s3_prefix_list_id    = module.network.s3_prefix_list_id
  db_security_group_id = module.database.security_group_id
  db_port              = module.database.port
  db_user_arns         = module.bench.db_user_arns

  container_definition = module.bench.container_definition
  execution_role_arn   = module.bench.execution_role_arn
  execution_role_name  = module.bench.execution_role_name
  task_cpu             = module.bench.task_cpu
  task_memory          = module.bench.task_memory

  cache = {
    file_system_id    = module.cache[0].file_system_id
    file_system_arn   = module.cache[0].file_system_arn
    access_point_id   = module.cache[0].access_point_id
    access_point_arn  = module.cache[0].access_point_arn
    security_group_id = module.cache[0].security_group_id
  }

  model_provider = var.study.model_provider
  model_region   = var.study.model_region
  workspace_id   = var.study.workspace_id
  api_key_secret = var.study.api_key_secret
}
