# The durable LLM cache (ADR-0042): an encrypted EFS file system the study task
# mounts where it used to write to scratch, and an hourly-or-slower DataSync
# task that copies it into the tier-0 recovery bucket (modules/recovery).
#
# Why the cache needs this at all. Every file under llm.cache_dir is a model
# call already paid for, and replay is the study's recovery path (M8). On a
# Fargate task the cache lived on scratch, which dies with the task -- so a
# fan-out stopped at its timeout, which is this design's own stop rule
# (modules/pipeline, fanout_timeout_seconds), threw away every answer it had
# bought. The DR runbook records what losing a cache cost this project once.
#
# Why EFS, and not "sync to S3 when the task ends":
#
# * The task that matters is the one that does NOT end cleanly. Step Functions
#   stops a timed-out task with SIGTERM and, at most 120 seconds later on
#   Fargate, SIGKILL. A sync of tens of thousands of objects does not fit in
#   that window, and a task killed for memory gets no window. S3-at-the-
#   boundary would preserve the cache in exactly the runs that needed it least.
# * cascade/llm/cache.py already writes each entry with write-fsync-rename. On
#   EFS an fsync is durable across AZs when it returns, so the unit of loss is
#   the one call in flight -- not the wave, which modules/pipeline's
#   simulate_wave used to have to document as "the unit of re-payment".
# * It needs no code: a directory is a directory. A sync step would be a shell
#   wrapper around the CLI, and the chains' alerts are built on the CLI's exit
#   code being the container's (modules/observability).
#
# The loss S3-sync risks would be *correct* -- the cache is content-addressed
# and idempotent, so a missing entry is a miss, re-asked and re-recorded -- but
# not *free*: the miss is billed again, against a simulate ceiling that holds
# only at the batch rate (ADR-0020). Acceptable for correctness, not for cost.
#
# What EFS does not give: a copy that survives `terraform destroy` of a sandbox
# built to be destroyed, a bad `rm`, or the region. That is the second half of
# this module. The copy lands in the recovery bucket as plain objects, under
# Object Lock and cross-region replication that already exist, where it can be
# restored to a new file system OR to a laptop -- which is where this project's
# one real loss happened. AWS Backup was the alternative: its recovery points
# restore only into EFS, in an AWS account, from a second locked store with its
# own key and copy rules to keep in step with the first.

data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  partition = data.aws_partition.current.partition
  region    = data.aws_region.current.region
  account   = data.aws_caller_identity.current.account_id

  # Under the bucket's llm-cache/ prefix, one directory per sandbox name: two
  # sandboxes must not interleave, and the archive's deny rules are by prefix.
  recovery_subdirectory = "/${var.recovery_prefix}/${var.name}/"
  recovery_objects_arn  = "${var.recovery_bucket_arn}/${var.recovery_prefix}/${var.name}/*"
}

# --- The file system -------------------------------------------------------------------

resource "aws_efs_file_system" "this" {
  #checkov:skip=CKV2_AWS_18:The durable copy is this module's DataSync task into the Object-Locked, cross-region-replicated recovery bucket; an AWS Backup vault would be a second tier-0 store that restores only into EFS (ADR-0042).
  creation_token = "${var.name}-llm-cache"
  encrypted      = true
  kms_key_id     = var.kms_key_arn

  # Elastic: billed per byte moved, nothing while idle. The cache is a few
  # hundred MB written over days; provisioned throughput would bill around the
  # clock for a rate this workload never reaches.
  throughput_mode  = "elastic"
  performance_mode = "generalPurpose"

  tags = { Name = "${var.name}-llm-cache" }
}

# Every recording is money already spent, and automatic backups are off only
# because the recovery bucket is the backup; say so where AWS can see it, so a
# later console default does not quietly add a second, unlocked copy.
resource "aws_efs_backup_policy" "this" {
  file_system_id = aws_efs_file_system.this.id
  backup_policy {
    status = "DISABLED"
  }
}

# One identity for every client, enforced by the access point rather than
# trusted from the container: the image runs as uid 10001
# (infra/docker/bench.Dockerfile), and a future image that does not still
# reads and writes the same files.
resource "aws_efs_access_point" "llm" {
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    uid = 10001
    gid = 10001
  }

  # Clients see /llm as their root and cannot walk above it.
  root_directory {
    path = "/llm"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "0750"
    }
  }

  tags = { Name = "${var.name}-llm-cache" }
}

# Three refusals that hold whatever an identity policy says: no plaintext NFS,
# nothing that did not come through a mount target in this VPC, and no root.
# Who MAY mount is granted on the roles (the study's task role, DataSync's
# read role), scoped to the access point; this policy is the floor under them.
data "aws_iam_policy_document" "file_system" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["*"]
    resources = [aws_efs_file_system.this.arn]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "OnlyThroughAnAccessPoint"
    effect    = "Deny"
    actions   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite", "elasticfilesystem:ClientRootAccess"]
    resources = [aws_efs_file_system.this.arn]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.llm.arn]
    }
  }

  statement {
    sid       = "NobodyIsRoot"
    effect    = "Deny"
    actions   = ["elasticfilesystem:ClientRootAccess"]
    resources = [aws_efs_file_system.this.arn]
    principals {
      type        = "AWS"
      identifiers = ["*"]
    }
  }
}

resource "aws_efs_file_system_policy" "this" {
  file_system_id = aws_efs_file_system.this.id
  policy         = data.aws_iam_policy_document.file_system.json
}

# --- Reaching it -----------------------------------------------------------------------------

resource "aws_security_group" "efs" {
  name        = "${var.name}-llm-cache"
  description = "NFS to the LLM cache, from the named client groups only"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-llm-cache" }
}

resource "aws_vpc_security_group_ingress_rule" "nfs" {
  for_each                     = merge(var.client_security_group_ids, { datasync = aws_security_group.datasync.id })
  security_group_id            = aws_security_group.efs.id
  description                  = "NFS from ${each.key}"
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  referenced_security_group_id = each.value
}

# One per isolated subnet. A client resolves the file system's name to the
# mount target in its own AZ, so an AZ with clients and no target cannot mount.
resource "aws_efs_mount_target" "this" {
  count           = length(var.subnet_ids)
  file_system_id  = aws_efs_file_system.this.id
  subnet_id       = var.subnet_ids[count.index]
  security_groups = [aws_security_group.efs.id]
}

# --- The copy that outlives the sandbox -----------------------------------------------------------

# DataSync's own network interfaces, in the isolated tier: they speak NFS to
# the mount target and nothing else. The S3 side of the transfer runs on
# DataSync's service network, not through these.
resource "aws_security_group" "datasync" {
  #checkov:skip=CKV2_AWS_5:Attached by ARN in aws_datasync_location_efs.cache's ec2_config -- DataSync holds it on the interfaces it creates; Checkov matches attachments by id and cannot see an ARN.
  name        = "${var.name}-llm-cache-sync"
  description = "DataSync interfaces: NFS to the LLM cache, no ingress"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-llm-cache-sync" }
}

resource "aws_vpc_security_group_egress_rule" "datasync_nfs" {
  security_group_id            = aws_security_group.datasync.id
  description                  = "NFS to the LLM cache"
  ip_protocol                  = "tcp"
  from_port                    = 2049
  to_port                      = 2049
  referenced_security_group_id = aws_security_group.efs.id
}

data "aws_iam_policy_document" "datasync_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["datasync.amazonaws.com"]
    }
    # datasync.amazonaws.com is one principal for every account's tasks; these
    # two keep another account's task from borrowing the roles below.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${local.partition}:datasync:${local.region}:${local.account}:*"]
    }
  }
}

# Reads the cache. Mount only -- no ClientWrite -- so the copy job cannot alter
# the thing it copies.
resource "aws_iam_role" "datasync_efs" {
  name               = "${var.name}-llm-cache-sync-read"
  description        = "DataSync: mount the LLM cache read-only through its access point"
  assume_role_policy = data.aws_iam_policy_document.datasync_assume.json
}

data "aws_iam_policy_document" "datasync_efs" {
  statement {
    sid       = "MountReadOnlyThroughTheAccessPoint"
    actions   = ["elasticfilesystem:ClientMount"]
    resources = [aws_efs_file_system.this.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.llm.arn]
    }
  }
}

resource "aws_iam_role_policy" "datasync_efs" {
  name   = "mount-read-only"
  role   = aws_iam_role.datasync_efs.id
  policy = data.aws_iam_policy_document.datasync_efs.json
}

# Writes the archive. The sandbox is the less trusted side of this transfer --
# it has the internet path and parses scraped text -- so its reach into tier 0
# is one prefix, add-only:
#
# * objects under llm-cache/<name>/ and nowhere else: not registry/, where the
#   labels are, and not another sandbox's directory;
# * NO s3:DeleteObject, which AWS's documented destination policy includes.
#   The task never deletes and never overwrites (below), and under Object Lock
#   a delete could only add a marker -- but tier 0 is the bucket nothing in a
#   sandbox may remove from, so the permission is withheld rather than left
#   unused. NOT verified live: if DataSync refuses a location whose role lacks
#   it, the refusal arrives at apply.
resource "aws_iam_role" "datasync_s3" {
  name               = "${var.name}-llm-cache-sync-write"
  description        = "DataSync: add LLM cache entries under one prefix of the recovery bucket"
  assume_role_policy = data.aws_iam_policy_document.datasync_assume.json
}

data "aws_iam_policy_document" "datasync_s3" {
  # Two statements, because s3:prefix exists only on ListBucket: a condition on
  # a key the request does not carry is false, and the other two would be
  # denied by the very statement that names them.
  statement {
    sid       = "FindTheBucket"
    actions   = ["s3:GetBucketLocation", "s3:ListBucketMultipartUploads"]
    resources = [var.recovery_bucket_arn]
  }
  statement {
    sid       = "ListThisPrefixOnly"
    actions   = ["s3:ListBucket"]
    resources = [var.recovery_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.recovery_prefix}/${var.name}/*", "${var.recovery_prefix}/${var.name}"]
    }
  }
  statement {
    sid       = "AddEntriesUnderThisPrefixOnly"
    actions   = ["s3:AbortMultipartUpload", "s3:GetObject", "s3:GetObjectTagging", "s3:ListMultipartUploadParts", "s3:PutObject", "s3:PutObjectTagging"]
    resources = [local.recovery_objects_arn]
  }
  # The recovery bucket is encrypted under the platform root's key, which is in
  # this account, so the grant lives here; through S3 only.
  statement {
    sid       = "UseTheRecoveryKeyThroughS3"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.recovery_kms_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${local.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "datasync_s3" {
  name   = "add-cache-entries"
  role   = aws_iam_role.datasync_s3.id
  policy = data.aws_iam_policy_document.datasync_s3.json
}

resource "aws_datasync_location_efs" "cache" {
  efs_file_system_arn         = aws_efs_file_system.this.arn
  access_point_arn            = aws_efs_access_point.llm.arn
  file_system_access_role_arn = aws_iam_role.datasync_efs.arn
  # The file system policy denies plaintext; DataSync must ask for TLS or be
  # refused by it.
  in_transit_encryption = "TLS1_2"

  ec2_config {
    subnet_arn          = "arn:${local.partition}:ec2:${local.region}:${local.account}:subnet/${var.subnet_ids[0]}"
    security_group_arns = ["arn:${local.partition}:ec2:${local.region}:${local.account}:security-group/${aws_security_group.datasync.id}"]
  }

  # DataSync mounts when the location is created.
  depends_on = [aws_efs_mount_target.this, aws_efs_file_system_policy.this, aws_iam_role_policy.datasync_efs]
}

resource "aws_datasync_location_s3" "recovery" {
  s3_bucket_arn = var.recovery_bucket_arn
  subdirectory  = local.recovery_subdirectory
  # Standard, not an infrequent-access class: those bill a minimum object size
  # far above a cache entry, and a minimum duration.
  s3_storage_class = "STANDARD"

  s3_config {
    bucket_access_role_arn = aws_iam_role.datasync_s3.arn
  }

  depends_on = [aws_iam_role_policy.datasync_s3]
}

# Named under /cascade/ so the sandbox key's CloudWatch Logs grant covers it.
resource "aws_cloudwatch_log_group" "sync" {
  #checkov:skip=CKV_AWS_338:Sandbox retention, deliberately short: the sandbox is created, measured and destroyed, and what must outlive it is the archive in the recovery bucket, not this log.
  name              = "/cascade/${var.name}/llm-cache-sync"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

# DataSync writes task logs as the service, which CloudWatch Logs admits
# through a resource policy rather than a role.
data "aws_iam_policy_document" "sync_logs" {
  statement {
    sid       = "DataSyncWritesThisGroup"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.sync.arn}:*"]
    principals {
      type        = "Service"
      identifiers = ["datasync.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

resource "aws_cloudwatch_log_resource_policy" "sync" {
  policy_name     = "${var.name}-llm-cache-sync"
  policy_document = data.aws_iam_policy_document.sync_logs.json
}

resource "aws_datasync_task" "archive" {
  name                     = "${var.name}-llm-cache-archive"
  source_location_arn      = aws_datasync_location_efs.cache.arn
  destination_location_arn = aws_datasync_location_s3.recovery.arn
  cloudwatch_log_group_arn = aws_cloudwatch_log_group.sync.arn

  # BASIC, and said out loud. Enhanced mode bills a fee per task EXECUTION on
  # top of the per-GB rate; on an hourly schedule that fee alone, over a month,
  # is more than this study's whole model budget. Basic bills per GB only.
  task_mode = "BASIC"

  # The decision this module refuses to make for the caller: see the variable.
  schedule {
    schedule_expression = var.sync_schedule_expression
  }

  options {
    # The archive only grows. An entry deleted from the file system -- by a
    # bad rm, or by the sandbox being destroyed -- stays in the archive, which
    # is what the archive is for.
    preserve_deleted_files = "PRESERVE"
    # An entry's name is the hash of what it answers, so an object that exists
    # is by definition the same answer. Never overwriting also means no second
    # version of anything under Object Lock.
    overwrite_mode = "NEVER"
    transfer_mode  = "CHANGED"
    # Verify what was sent, not the whole archive: a full verification GETs
    # every object on every run, and the archive is read far more often by
    # this task than by anyone restoring from it.
    verify_mode = "ONLY_FILES_TRANSFERRED"
    # POSIX ownership means nothing in S3, and the access point re-imposes it
    # on the way back.
    posix_permissions = "NONE"
    uid               = "NONE"
    gid               = "NONE"
    log_level         = "BASIC"
  }

  # cache.py writes ".<key>-xxxx.tmp" and renames it into place. A half-written
  # temporary must never reach a bucket where it could not be deleted.
  excludes {
    filter_type = "SIMPLE_PATTERN"
    value       = "*.tmp"
  }

  depends_on = [aws_cloudwatch_log_resource_policy.sync]
}

# --- A copy job that fails silently is worse than none ------------------------------------------------

# Named "<name>-task-..." on purpose: the platform root admits this sandbox's
# EventBridge rules to the alerts topic by that prefix (envs/platform,
# SandboxTaskFailureRulesPublish), so no second topic-policy statement has to
# be kept in step across two roots.
resource "aws_cloudwatch_event_rule" "sync_failed" {
  count       = var.alerts_topic_arn == null ? 0 : 1
  name        = "${var.name}-task-llm-cache-sync-failed"
  description = "The LLM cache's DataSync execution ended in ERROR: the recovery copy is older than the schedule says"

  event_pattern = jsonencode({
    source        = ["aws.datasync"]
    "detail-type" = ["DataSync Task Execution State Change"]
    resources     = [{ prefix = aws_datasync_task.archive.arn }]
    detail        = { State = ["ERROR"] }
  })
}

resource "aws_cloudwatch_event_target" "sync_failed" {
  count     = var.alerts_topic_arn == null ? 0 : 1
  rule      = aws_cloudwatch_event_rule.sync_failed[0].name
  target_id = "alerts"
  arn       = var.alerts_topic_arn
}
