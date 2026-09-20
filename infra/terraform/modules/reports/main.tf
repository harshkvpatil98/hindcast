# Where the study's deliverable lands (ADR-0046). `cascade report` writes
# Appendix D as a directory -- `headline.md`, `manifest.json`, `metrics.json`,
# five CSVs, `significance.json`, `leakage_report.json`, `cost_ledger.json` and
# four SVG figures -- and until now nothing published it: it existed in a
# task's scratch storage, which dies with the task.
#
# **The report carries the labels.** This is the fact the design turns on, and
# it is easy to miss because the report reads like a summary. `baselines.csv`
# is (config_id, scenario_id, p_hat, outcome) and `ablation_grid.csv` is
# (cell_id, scenario_id, p_hat, sigma, outcome): one row per scenario, with
# how it resolved. So this bucket is a label-bearing store in the sense
# modules/recovery already uses, and two consequences follow.
#
#   1. The simulation is denied reading it, by the same resource-policy Deny
#      that guards the registry archive (invariant 2). The study task WRITES
#      here -- it is the process that runs `cascade report` -- and cannot read
#      a report back. A simulation that could read a previous report could
#      read the outcome column out of `ablation_grid.csv`, which is precisely
#      the leak the Postgres grant and the recovery bucket's Deny exist to
#      prevent. Write-only is not an accident of scoping here; it is the
#      requirement.
#   2. Nothing is served publicly. See below.
#
# --- Served, or fetched? CloudFront + OAC is REJECTED ---------------------------------
#
# The two candidates were a static site (CloudFront distribution, origin
# access control, bucket stays private) and objects fetched directly by a
# named reader -- `aws s3 sync`, or a presigned URL for one file. This module
# implements the second and refuses the first, for three reasons, in the order
# they matter:
#
# * A distribution is a standing public endpoint for a directory whose CSVs
#   state, per scenario, how each question resolved. §1.3's frozen split and
#   §4.4's memorisation probe both rest on the labels not being crawlable:
#   publishing this study's own answer key on the open web is a leakage vector
#   for the next run of this study, and for anyone else's. CloudFront can be
#   closed with signed URLs and a key group -- which is the presigned-URL model
#   rebuilt with a key pair, a trusted key group and a rotation story to own.
# * A presigned URL made by an assumed role expires when the role session
#   expires, whatever expiry was asked for (AWS: "IAM role credentials -- the
#   presigned URL expires when the role session expires, even if you specify a
#   longer expiration time"). Short-lived by construction is the right default
#   for a bearer token over label-bearing data. A distribution is the opposite:
#   it exists until someone deletes it.
# * The artifact is a directory read by a handful of people a handful of
#   times. A distribution bills and must be maintained between those times,
#   and `headline.md` is Markdown -- a browser would download it, not render
#   it, so the "site" would not even be a site without a generator this
#   project does not have.
#
# What the rejection costs, stated rather than hidden: the SVG figures are
# referenced from `headline.md` by relative name, so a per-object presigned
# URL does not render them inline. The reader takes the directory whole
# (`aws s3 sync`) and opens it locally, which is how a Markdown-plus-CSV
# artifact is read anyway. Presigned URLs are for handing one file to one
# person who cannot assume a role.

locals {
  bucket_name = "${var.name}-reports-${var.bucket_suffix}"

  # One prefix, named once here and handed to every writer as an output, so a
  # writer's IAM scope and this bucket's Deny rules cannot come to disagree
  # about a name -- the same rule modules/recovery follows for its three.
  prefix = "reports"

  objects_arn = "${aws_s3_bucket.this.arn}/${local.prefix}/*"
}

resource "aws_s3_bucket" "this" {
  #checkov:skip=CKV_AWS_18:Access logging is CloudTrail data events from the audit module, which the platform root points at this bucket; a logging bucket per bucket multiplies what must itself be protected.
  #checkov:skip=CKV_AWS_144:A report is tier 3 in docs/architecture/dr-runbook.md -- derived, and recovered by re-running `cascade report` against the sealed split. Tier 0 is the one place cross-region replication earns its cost.
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events from the reports bucket; a written report is read by a person, not by a pipeline.
  #checkov:skip=CKV2_AWS_61:No lifecycle rule on purpose: versions here are under Object Lock, and an expiry rule is a standing attempt to delete the published claim.
  bucket = local.bucket_name

  # Object Lock is decided, not inherited. See var.report_lock_retention_days
  # for why a report is the one derived artifact that is worth locking, and
  # what it costs operationally.
  object_lock_enabled = true
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = "Enabled"
  }
}

# GOVERNANCE, not COMPLIANCE: nobody can undo COMPLIANCE, and a published
# figure that has to be withdrawn for a reason nobody anticipated is a
# situation this project should be able to get out of by a deliberate,
# recorded act -- not by deleting the account. modules/eventlake made the same
# choice for the same reason.
resource "aws_s3_bucket_object_lock_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = var.report_lock_retention_days
    }
  }
  depends_on = [aws_s3_bucket_versioning.this]
}

resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# All four, and a test asserts all four. "Not a public bucket" is the single
# claim this module would be worst at being wrong about.
resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
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

  # A report is a claim. Object Lock stops a version being removed; this stops
  # the lock itself being lifted, by anyone, including the account root -- so
  # withdrawing a published figure costs two acts, editing this policy and
  # then deleting, and the trail records both because the platform root lists
  # this bucket among its data events. It is the Config bucket's argument
  # (ADR-0035) applied ON TOP of a lock rather than instead of one.
  #
  # What this does NOT claim: Object Lock protects versions, not keys. AWS is
  # explicit that retention "doesn't prevent new versions of the object from
  # being created". Re-uploading `headline.md` writes a new version; the old
  # one survives and is what a reader can still fetch. Immutability here means
  # the history is complete, not that the latest bytes never change.
  statement {
    sid    = "NoVersionIsEverRemovedAndNoLockIsEverLifted"
    effect = "Deny"
    actions = [
      "s3:DeleteObjectVersion",
      "s3:BypassGovernanceRetention",
      "s3:PutObjectRetention",
      "s3:PutObjectLegalHold",
      "s3:PutBucketObjectLockConfiguration",
    ]
    resources = [aws_s3_bucket.this.arn, "${aws_s3_bucket.this.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }

  # Invariant 2, on a bucket, for the third store that holds the labels.
  # `baselines.csv` and `ablation_grid.csv` carry an `outcome` column per
  # scenario, so the process that WRITES the report is denied reading one.
  dynamic "statement" {
    for_each = length(var.simulation_principal_arns) > 0 ? [1] : []
    content {
      sid       = "SimulationNeverReadsReports"
      effect    = "Deny"
      actions   = ["s3:GetObject", "s3:GetObjectVersion"]
      resources = [local.objects_arn]
      principals {
        type        = "AWS"
        identifiers = var.simulation_principal_arns
      }
    }
  }
}

resource "aws_s3_bucket_policy" "this" {
  bucket     = aws_s3_bucket.this.id
  policy     = data.aws_iam_policy_document.bucket.json
  depends_on = [aws_s3_bucket_public_access_block.this]
}

# --- Who may read a report --------------------------------------------------------------
#
# A managed policy, attached to nobody here. Who the readers are is the
# account owner's decision -- the same shape modules/recovery uses for the
# archivist -- and stating it in Terraform would put a list of people in a
# repository that is about forecasting. What this DOES decide is the shape of
# the answer: reading a report is an authenticated act by a named principal,
# never an anonymous fetch, and it is read-only: a reader cannot add a report,
# amend one, or remove a version.
#
# `aws s3 sync s3://<bucket>/reports/<study>/ ./study/` is the intended
# command. `aws s3 presign` on one object is the other, for handing a single
# file to someone who cannot assume a role; it inherits this policy's limits
# and, from an assumed role, the session's lifetime.
data "aws_iam_policy_document" "read" {
  statement {
    sid       = "ReadPublishedReports"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = [local.objects_arn]
  }
  # `aws s3 sync` lists the source to decide what to fetch. Names only, and
  # only under the reports prefix.
  statement {
    sid       = "ListWhatIsPublished"
    actions   = ["s3:ListBucket", "s3:ListBucketVersions"]
    resources = [aws_s3_bucket.this.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${local.prefix}/*", local.prefix]
    }
  }
  statement {
    sid       = "DecryptThroughS3"
    actions   = ["kms:Decrypt"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_policy" "read" {
  name        = "${var.name}-reports-read"
  description = "Read published study reports: no write, no delete, no lock override"
  policy      = data.aws_iam_policy_document.read.json
}
