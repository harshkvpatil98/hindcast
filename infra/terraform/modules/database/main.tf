# Aurora PostgreSQL 16 with pgvector: the Chronofence store (M3), moved off a
# 7.75 GB laptop container whose memory ceiling M8 measured as the binding
# constraint on retrieval p95.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  account   = data.aws_caller_identity.current.account_id
  partition = data.aws_partition.current.partition
  region    = data.aws_region.current.region
  log_group = "/aws/rds/cluster/${var.name}/postgresql"
}

# --- Encryption ----------------------------------------------------------------

data "aws_iam_policy_document" "key" {
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

  # The exported PostgreSQL log group is encrypted with this key too.
  statement {
    sid       = "CloudWatchLogsForThisCluster"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${local.region}:${local.account}:log-group:${local.log_group}"]
    }
  }
}

resource "aws_kms_key" "data" {
  description             = "${var.name}: Aurora storage, snapshots, secrets, Performance Insights, logs"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.key.json
}

resource "aws_kms_alias" "data" {
  name          = "alias/${var.name}-data"
  target_key_id = aws_kms_key.data.key_id
}

# --- Network placement ---------------------------------------------------------

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = var.subnet_ids
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "PostgreSQL from named client security groups only"
  vpc_id      = var.vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "postgres" {
  for_each                     = var.client_security_group_ids
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from ${each.key}"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = each.value
}

# --- Parameters ------------------------------------------------------------------

resource "aws_rds_cluster_parameter_group" "this" {
  name        = "${var.name}-pg16"
  family      = "aurora-postgresql16"
  description = "Cascade: TLS required; slow-statement and connection logging"

  # Plaintext connections are refused. cascade/config.py's database_url()
  # carries sslmode for the same reason (M11).
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  # Logs any statement over 1 s. The Chronofence bench is measured in
  # milliseconds, so this records only pathological plans, not the workload.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

# --- Monitoring role -------------------------------------------------------------

data "aws_iam_policy_document" "monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "monitoring" {
  name               = "${var.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.monitoring_assume.json
}

resource "aws_iam_role_policy_attachment" "monitoring" {
  role       = aws_iam_role.monitoring.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# --- Cluster -------------------------------------------------------------------

# Created before the cluster so the exported logs get a retention and a key
# rather than the never-expiring, unencrypted group RDS would create itself.
resource "aws_cloudwatch_log_group" "postgresql" {
  #checkov:skip=CKV_AWS_338:Sandbox retention, deliberately short: the environment is created, measured and destroyed. M12's production design sets 365 days.
  name              = local.log_group
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.data.arn
}

resource "aws_rds_cluster" "this" {
  #checkov:skip=CKV2_AWS_8:Aurora's continuous backups and point-in-time restore cover this cluster (backup_retention_period); an organisation-wide AWS Backup plan is M12's governance design.
  #checkov:skip=CKV2_AWS_27:log_statement=all would add per-query I/O to the very latency the bench measures; slow statements are logged through log_min_duration_statement.
  cluster_identifier = var.name
  engine             = "aurora-postgresql"
  engine_mode        = "provisioned"
  engine_version     = var.engine_version
  database_name      = var.database_name
  port               = 5432

  master_username = var.master_username
  # The master password is generated, stored and rotated by RDS in Secrets
  # Manager; it never appears in Terraform state.
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.data.arn

  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.data.arn
  iam_database_authentication_enabled = true

  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  serverlessv2_scaling_configuration {
    min_capacity = var.min_acu
    max_capacity = var.max_acu
  }

  backup_retention_period         = var.backup_retention_days
  preferred_backup_window         = "07:00-08:00"
  preferred_maintenance_window    = "sun:08:30-sun:09:30"
  copy_tags_to_snapshot           = true
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = var.skip_final_snapshot
  final_snapshot_identifier       = var.skip_final_snapshot ? null : "${var.name}-final"
  enabled_cloudwatch_logs_exports = ["postgresql"]
  allow_major_version_upgrade     = false

  lifecycle {
    precondition {
      condition     = var.min_acu <= var.max_acu
      error_message = "min_acu must not exceed max_acu."
    }
  }

  depends_on = [aws_cloudwatch_log_group.postgresql]
}

resource "aws_rds_cluster_instance" "this" {
  #checkov:skip=CKV_AWS_226:Off on purpose: the Aurora minor version decides the pgvector version, and the benchmark compares against pgvector 0.8.0 (ADR-0034).
  count              = var.instance_count
  identifier         = "${var.name}-${count.index}"
  cluster_identifier = aws_rds_cluster.this.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.this.engine
  engine_version     = aws_rds_cluster.this.engine_version

  publicly_accessible = false
  # Off so pgvector cannot move under the benchmark; see engine_version.
  auto_minor_version_upgrade = false

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.data.arn
  performance_insights_retention_period = 7
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.monitoring.arn
  copy_tags_to_snapshot                 = true
}

# --- Application role passwords -------------------------------------------------

# Migration 001 creates cascade_sim and cascade_eval with passwords, and the
# CLI connects with them today. IAM authentication is enabled on the cluster
# for the move to token-based login; until then each role's password is
# generated here and held in Secrets Manager. It is also in Terraform state,
# which is why the state bucket is encrypted with its own CMK (envs/bootstrap).
resource "random_password" "role" {
  for_each = var.app_roles
  length   = 40
  special  = true
  # psql variables quote the value, but a quote or backslash in a password is
  # still the kind of thing that breaks a migration at 2 a.m.
  override_special = "-_.~"
}

resource "aws_secretsmanager_secret" "role" {
  #checkov:skip=CKV2_AWS_57:Rotating these would desynchronise the roles migration 001 created; the rotation path is IAM database authentication (enabled on the cluster), not password rotation. ADR-0034.
  for_each                = var.app_roles
  name                    = "${var.name}/${each.key}"
  description             = "Password for Postgres role ${each.key} on ${var.name}"
  kms_key_id              = aws_kms_key.data.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "role" {
  for_each  = var.app_roles
  secret_id = aws_secretsmanager_secret.role[each.key].id
  secret_string = jsonencode({
    username = each.key
    password = random_password.role[each.key].result
    host     = aws_rds_cluster.this.endpoint
    port     = aws_rds_cluster.this.port
    dbname   = var.database_name
  })
}

# --- Experiment clone ---------------------------------------------------------------

resource "aws_rds_cluster" "clone" {
  #checkov:skip=CKV_AWS_139:A disposable copy-on-write clone for one experiment; deletion protection would only obstruct its teardown.
  #checkov:skip=CKV2_AWS_8:As above -- nothing on the clone needs keeping.
  count              = var.experiment_clone ? 1 : 0
  cluster_identifier = "${var.name}-clone"
  engine             = aws_rds_cluster.this.engine
  engine_mode        = "provisioned"
  engine_version     = aws_rds_cluster.this.engine_version

  restore_to_point_in_time {
    source_cluster_identifier  = aws_rds_cluster.this.cluster_identifier
    restore_type               = "copy-on-write"
    use_latest_restorable_time = true
  }

  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.data.arn
  iam_database_authentication_enabled = true
  db_subnet_group_name                = aws_db_subnet_group.this.name
  vpc_security_group_ids              = [aws_security_group.db.id]
  db_cluster_parameter_group_name     = aws_rds_cluster_parameter_group.this.name
  copy_tags_to_snapshot               = true
  deletion_protection                 = false
  skip_final_snapshot                 = true
  backup_retention_period             = 1
  enabled_cloudwatch_logs_exports     = ["postgresql"]

  serverlessv2_scaling_configuration {
    min_capacity = var.min_acu
    max_capacity = var.max_acu
  }
}

resource "aws_rds_cluster_instance" "clone" {
  #checkov:skip=CKV_AWS_226:Off on purpose: the Aurora minor version decides the pgvector version, and the benchmark compares against pgvector 0.8.0 (ADR-0034).
  count                                 = var.experiment_clone ? 1 : 0
  identifier                            = "${var.name}-clone-0"
  cluster_identifier                    = aws_rds_cluster.clone[0].id
  instance_class                        = "db.serverless"
  engine                                = aws_rds_cluster.clone[0].engine
  engine_version                        = aws_rds_cluster.clone[0].engine_version
  publicly_accessible                   = false
  auto_minor_version_upgrade            = false
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.data.arn
  performance_insights_retention_period = 7
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.monitoring.arn
  copy_tags_to_snapshot                 = true
}
