# The event lake: the simulation's append-only decision log (invariant 6),
# exported as Parquet and queryable with Athena.
#
# In Postgres, append-only is a grant: cascade_sim holds SELECT and INSERT on
# `events` and nothing else (migration 009). Here it is two things at once --
# S3 Object Lock, under which no version of an object can be overwritten or
# deleted during its retention, and a writer role that is explicitly denied
# every delete and every lock override. Either alone would do; both means a
# single misconfiguration cannot make the log mutable.
#
# Resolution labels never come here. The lake holds what the simulation did,
# not what happened in the world, so invariant 2 -- the simulation never reads
# the labels -- needs no rule in this module: there is nothing to read.

data "aws_iam_policy_document" "assume_from_account" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  glue_arn = "arn:${data.aws_partition.current.partition}:glue:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}"
}

# --- The locked bucket ---------------------------------------------------------------

resource "aws_s3_bucket" "events" {
  #checkov:skip=CKV_AWS_18:Access logging for the lake is CloudTrail data events, in the platform's audit design; a second logging bucket per bucket multiplies what must itself be protected.
  #checkov:skip=CKV_AWS_144:Replication of a locked bucket is a disaster-recovery decision, taken in docs/architecture/dr-runbook.md, not a default.
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events from the lake.
  #checkov:skip=CKV2_AWS_61:No lifecycle rule on purpose: objects here are under Object Lock, and an expiry rule is a standing attempt to delete them.
  bucket              = "${var.name}-events-${var.bucket_suffix}"
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "events" {
  bucket = aws_s3_bucket.events.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "events" {
  bucket = aws_s3_bucket.events.id
  rule {
    default_retention {
      mode = var.retention_mode
      days = var.retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.events]
}

resource "aws_s3_bucket_ownership_controls" "events" {
  bucket = aws_s3_bucket.events.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "events" {
  bucket                  = aws_s3_bucket.events.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "events" {
  bucket = aws_s3_bucket.events.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "events_bucket" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.events.arn, "${aws_s3_bucket.events.arn}/*"]
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

resource "aws_s3_bucket_policy" "events" {
  bucket = aws_s3_bucket.events.id
  policy = data.aws_iam_policy_document.events_bucket.json
}

# --- Query results: short-lived, never locked -------------------------------------------

resource "aws_s3_bucket" "results" {
  #checkov:skip=CKV_AWS_18:Query results are disposable and expire in 7 days.
  #checkov:skip=CKV_AWS_144:Disposable query results are not replicated.
  #checkov:skip=CKV_AWS_21:Versioning a bucket of 7-day query results would keep what the lifecycle rule exists to remove.
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events from query results.
  bucket        = "${var.name}-athena-results-${var.bucket_suffix}"
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "results" {
  bucket = aws_s3_bucket.results.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket                  = aws_s3_bucket.results.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  bucket = aws_s3_bucket.results.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# --- Catalog: the schema of migration 009's `events`, as Parquet -----------------------------

resource "aws_glue_catalog_database" "this" {
  name = replace(var.name, "-", "_")
}

resource "aws_glue_catalog_table" "events" {
  name          = "events"
  database_name = aws_glue_catalog_database.this.name
  table_type    = "EXTERNAL_TABLE"
  parameters    = { classification = "parquet", EXTERNAL = "TRUE" }

  # One prefix per ablation cell: every analysis in the study compares cells,
  # so this is the partition that prunes.
  partition_keys {
    name = "config_id"
    type = "string"
  }

  storage_descriptor {
    location      = "s3://${aws_s3_bucket.events.bucket}/events/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
    }

    dynamic "columns" {
      for_each = [
        ["run_id", "string"], ["step", "smallint"], ["seq", "smallint"],
        ["actor_id", "string"], ["obs_hash", "binary"], ["action", "string"],
        ["caused_by", "string"], ["factor_delta", "string"], ["cache_hit", "boolean"],
        ["tokens_in", "int"], ["tokens_out", "int"], ["latency_ms", "int"],
        ["coercion", "string"],
      ]
      content {
        name = columns.value[0]
        type = columns.value[1]
      }
    }
  }
}

resource "aws_athena_workgroup" "this" {
  name          = var.name
  force_destroy = true

  configuration {
    # Workgroup settings win over whatever a client asks for: nobody can run a
    # query unencrypted, or without the scan limit, by changing a client flag.
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = var.query_scan_limit_bytes

    result_configuration {
      output_location = "s3://${aws_s3_bucket.results.bucket}/"
      encryption_configuration {
        encryption_option = "SSE_KMS"
        kms_key_arn       = var.kms_key_arn
      }
    }
  }
}

# --- Roles: the Postgres role split, again ---------------------------------------------------

resource "aws_iam_role" "writer" {
  name               = "${var.name}-lake-writer"
  description        = "The simulation's export: append events, nothing else"
  assume_role_policy = data.aws_iam_policy_document.assume_from_account.json
}

data "aws_iam_policy_document" "writer" {
  statement {
    sid       = "AppendEvents"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.events.arn}/events/*"]
  }
  statement {
    sid       = "EncryptThem"
    actions   = ["kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
  # Explicit denies outrank any allow attached later. Append-only, by
  # construction: no delete, and no shortening or bypassing of a lock.
  statement {
    sid    = "NeverDeleteNeverUnlock"
    effect = "Deny"
    actions = [
      "s3:BypassGovernanceRetention",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutBucketObjectLockConfiguration",
      "s3:PutObjectLegalHold",
      "s3:PutObjectRetention",
    ]
    resources = [aws_s3_bucket.events.arn, "${aws_s3_bucket.events.arn}/*"]
  }
}

resource "aws_iam_role_policy" "writer" {
  name   = "append-only"
  role   = aws_iam_role.writer.id
  policy = data.aws_iam_policy_document.writer.json
}

resource "aws_iam_role" "analyst" {
  name               = "${var.name}-lake-analyst"
  description        = "Evaluation: query events through the workgroup, read-only"
  assume_role_policy = data.aws_iam_policy_document.assume_from_account.json
}

data "aws_iam_policy_document" "analyst" {
  statement {
    sid       = "QueryThroughTheWorkgroupOnly"
    actions   = ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:GetQueryResults", "athena:StopQueryExecution"]
    resources = [aws_athena_workgroup.this.arn]
  }
  # Glue authorises a table read against the catalog, the database and the
  # table together, so all three are named -- and nothing else in the catalog.
  statement {
    sid     = "ReadThisTableOnly"
    actions = ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"]
    resources = [
      "${local.glue_arn}:catalog",
      "${local.glue_arn}:database/${aws_glue_catalog_database.this.name}",
      "${local.glue_arn}:table/${aws_glue_catalog_database.this.name}/${aws_glue_catalog_table.events.name}",
    ]
  }
  statement {
    sid       = "ReadEvents"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.events.arn, "${aws_s3_bucket.events.arn}/*"]
  }
  statement {
    sid       = "ResultsReadWrite"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.results.arn, "${aws_s3_bucket.results.arn}/*"]
  }
  statement {
    sid       = "Keys"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_role_policy" "analyst" {
  name   = "query"
  role   = aws_iam_role.analyst.id
  policy = data.aws_iam_policy_document.analyst.json
}
