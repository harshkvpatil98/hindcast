# The remote-state bucket for every other root. Versioned, encrypted with its
# own CMK (sandbox state holds generated role passwords), TLS-only, and never
# public.

variable "region" {
  description = "AWS region. Required, never defaulted (ADR-0028)."
  type        = string
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "cascade", ManagedBy = "terraform", Purpose = "terraform-state" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  bucket = "cascade-tfstate-${data.aws_caller_identity.current.account_id}-${var.region}"
}

data "aws_iam_policy_document" "state_key" {
  #checkov:skip=CKV_AWS_111:A KMS key policy's "kms:*" for the account root is AWS's default key policy: it delegates to IAM, and Resource "*" in a key policy means this key only.
  #checkov:skip=CKV_AWS_356:Resource "*" in a key policy refers to the key itself, not to all resources.
  #checkov:skip=CKV_AWS_109:As above -- the account-root statement is the standard delegation to IAM.
  statement {
    sid       = "AccountAdministers"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_kms_key" "state" {
  description             = "cascade: Terraform state"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  # Explicit rather than implied: the default policy is the same text, but a
  # key whose policy is written down can be reviewed.
  policy = data.aws_iam_policy_document.state_key.json
}

resource "aws_s3_bucket" "state" {
  #checkov:skip=CKV_AWS_18:Access logs for the state bucket would need a second bucket and are covered by CloudTrail in M12.
  #checkov:skip=CKV_AWS_144:State is small and versioned; cross-region replication is a disaster-recovery decision for M12's DR design.
  #checkov:skip=CKV2_AWS_62:Nothing consumes state-bucket events.
  bucket = local.bucket
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.state.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "noncurrent"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

data "aws_iam_policy_document" "state" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
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

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state.json
}

output "backend_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "backend_kms_key_arn" {
  value = aws_kms_key.state.arn
}
