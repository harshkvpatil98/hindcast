# Tier-0 recovery (docs/architecture/dr-runbook.md): the datasets that cannot
# be regenerated -- the sealed registry archive (`cascade ledger export`), the
# source-response cache that reproduces it, and the LLM recording cache, where
# every file is a model call already paid for.
#
# This project lost the first two once, with a local volume, and the frozen
# split with them. So this bucket is versioned, Object-Locked, and replicated
# to a second region: tier 0 is the one place cross-region replication earns
# its cost, because a region loss must cost time and never the split.

data "aws_region" "primary" {}

data "aws_region" "replica" {
  provider = aws.replica
}

locals {
  primary_name = "${var.name}-recovery-${var.bucket_suffix}-${data.aws_region.primary.region}"
  replica_name = "${var.name}-recovery-${var.bucket_suffix}-${data.aws_region.replica.region}"
}

# --- Primary ---------------------------------------------------------------------

resource "aws_s3_bucket" "primary" {
  #checkov:skip=CKV_AWS_18:Access logging is CloudTrail data events from the audit module; a logging bucket per bucket multiplies what must itself be protected.
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events from the recovery bucket.
  #checkov:skip=CKV2_AWS_61:No lifecycle rule on purpose: objects are under Object Lock, and an expiry rule is a standing attempt to delete them.
  bucket              = local.primary_name
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "primary" {
  bucket = aws_s3_bucket.primary.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "primary" {
  bucket = aws_s3_bucket.primary.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.primary]
}

resource "aws_s3_bucket_ownership_controls" "primary" {
  bucket = aws_s3_bucket.primary.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "primary" {
  bucket                  = aws_s3_bucket.primary.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "primary" {
  bucket = aws_s3_bucket.primary.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "primary_bucket" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.primary.arn, "${aws_s3_bucket.primary.arn}/*"]
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

  # Invariant 2, on a bucket. A Deny in a resource policy outranks any allow
  # the principal's own policies carry, now or later.
  dynamic "statement" {
    for_each = length(var.simulation_principal_arns) > 0 ? [1] : []
    content {
      sid       = "SimulationNeverReadsLabels"
      effect    = "Deny"
      actions   = ["s3:GetObject", "s3:GetObjectVersion"]
      resources = ["${aws_s3_bucket.primary.arn}/registry/*"]
      principals {
        type        = "AWS"
        identifiers = var.simulation_principal_arns
      }
    }
  }
}

resource "aws_s3_bucket_policy" "primary" {
  bucket = aws_s3_bucket.primary.id
  policy = data.aws_iam_policy_document.primary_bucket.json
}

# --- Replica, in the second region -----------------------------------------------------

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_iam_policy_document" "replica_key" {
  #checkov:skip=CKV_AWS_111:A KMS key policy's "kms:*" for the account root is AWS's default key policy: it delegates to IAM, and Resource "*" in a key policy means this key only.
  #checkov:skip=CKV_AWS_356:Resource "*" in a key policy refers to the key itself, not to all resources.
  #checkov:skip=CKV_AWS_109:As above -- the account-root statement is the standard delegation to IAM.
  statement {
    sid       = "AccountAdministers"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

# KMS keys are regional: the replica needs one of its own.
resource "aws_kms_key" "replica" {
  provider                = aws.replica
  description             = "${var.name}: tier-0 recovery replica"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.replica_key.json
}

resource "aws_s3_bucket" "replica" {
  #checkov:skip=CKV_AWS_18:As the primary: CloudTrail data events are the access record.
  #checkov:skip=CKV_AWS_144:This bucket IS the cross-region replica.
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events from the replica.
  #checkov:skip=CKV2_AWS_61:No lifecycle rule on purpose: objects are under Object Lock.
  provider            = aws.replica
  bucket              = local.replica_name
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "replica" {
  provider = aws.replica
  bucket   = aws_s3_bucket.replica.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "replica" {
  provider = aws.replica
  bucket   = aws_s3_bucket.replica.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.replica]
}

resource "aws_s3_bucket_ownership_controls" "replica" {
  provider = aws.replica
  bucket   = aws_s3_bucket.replica.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "replica" {
  provider                = aws.replica
  bucket                  = aws_s3_bucket.replica.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "replica" {
  provider = aws.replica
  bucket   = aws_s3_bucket.replica.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.replica.arn
    }
    bucket_key_enabled = true
  }
}

# --- Replication -------------------------------------------------------------------------

data "aws_iam_policy_document" "replication_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "replication" {
  name               = "${var.name}-recovery-replication"
  assume_role_policy = data.aws_iam_policy_document.replication_assume.json
}

data "aws_iam_policy_document" "replication" {
  statement {
    sid       = "ReadSource"
    actions   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
    resources = [aws_s3_bucket.primary.arn]
  }
  statement {
    sid       = "ReadSourceVersions"
    actions   = ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectVersionTagging", "s3:GetObjectRetention", "s3:GetObjectLegalHold"]
    resources = ["${aws_s3_bucket.primary.arn}/*"]
  }
  statement {
    sid       = "WriteReplica"
    actions   = ["s3:ReplicateObject", "s3:ReplicateTags"]
    resources = ["${aws_s3_bucket.replica.arn}/*"]
  }
  statement {
    sid       = "DecryptSource"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
  statement {
    sid       = "EncryptReplica"
    actions   = ["kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.replica.arn]
  }
}

resource "aws_iam_role_policy" "replication" {
  name   = "replicate"
  role   = aws_iam_role.replication.id
  policy = data.aws_iam_policy_document.replication.json
}

resource "aws_s3_bucket_replication_configuration" "this" {
  bucket = aws_s3_bucket.primary.id
  role   = aws_iam_role.replication.arn

  rule {
    id     = "tier-0"
    status = "Enabled"
    filter {}

    # A delete in the primary must not propagate: the replica exists for the
    # day something is deleted that should not have been.
    delete_marker_replication {
      status = "Disabled"
    }

    source_selection_criteria {
      sse_kms_encrypted_objects {
        status = "Enabled"
      }
    }

    destination {
      bucket = aws_s3_bucket.replica.arn
      encryption_configuration {
        replica_kms_key_id = aws_kms_key.replica.arn
      }
    }
  }

  depends_on = [aws_s3_bucket_versioning.primary, aws_s3_bucket_versioning.replica]
}
