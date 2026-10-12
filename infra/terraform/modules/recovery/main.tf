# Tier-0 recovery (docs/architecture/dr-runbook.md): the datasets that cannot
# be regenerated -- the sealed registry archive (`cascade ledger export`), the
# source-response cache that reproduces it, and the LLM recording cache, where
# every file is a model call already paid for.
#
# How each gets here (ADR-0042): the LLM cache is copied from the study's file
# system by a scheduled DataSync task (modules/cache, in the sandbox root),
# whose role can add objects under llm-cache/<sandbox>/ and nothing else. The
# registry archive and the source cache are made on a person's machine and
# uploaded by that person, with the add-only policy below. One replication
# rule, with no filter, carries all three to the second region.
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
  primary_name   = "${var.name}-recovery-${var.bucket_suffix}-${data.aws_region.primary.region}"
  replica_name   = "${var.name}-recovery-${var.bucket_suffix}-${data.aws_region.replica.region}"
  inventory_name = "${var.name}-recovery-inventory-${var.bucket_suffix}-${data.aws_region.primary.region}"

  # The three tier-0 datasets, one prefix each. Defined once, here, and handed
  # to everything that writes to them as outputs, so a writer's IAM scope and
  # this bucket's deny rules cannot come to disagree about a name.
  prefixes = {
    registry     = "registry"     # `cascade ledger export` archives. Holds the labels.
    source_cache = "source-cache" # ledger.source_cache_dir: the raw API responses a sealed split is re-derived from.
    llm_cache    = "llm-cache"    # llm.cache_dir: every file a model call already paid for (modules/cache writes it).
  }

  # Where the resolution labels are. The registry archive, obviously -- and the
  # source cache, which is easy to miss: it is the markets' own API responses,
  # and a resolved market's response states how it resolved. The LLM cache is
  # not here on purpose. It is what the simulation itself recorded, from
  # prompts invariant 2 already kept label-free.
  label_bearing_prefixes = [local.prefixes.registry, local.prefixes.source_cache]

  # A sha256 in hex, as a run of "match any single character" wildcards. IAM
  # resource ARNs support `*` and `?`, where `?` is exactly one character
  # (IAM user guide, "Using wildcards in resource ARNs"), so the archivist's
  # grant can require that the FIRST path segment under each uploaded prefix
  # be 64 characters wide -- which is the manifest sha256 the archive belongs
  # to. See `archive_write` below for why that is worth enforcing rather than
  # documenting.
  sha_glob = join("", [for _ in range(64) : "?"])
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
      resources = [for prefix in local.label_bearing_prefixes : "${aws_s3_bucket.primary.arn}/${prefix}/*"]
      principals {
        type        = "AWS"
        identifiers = var.simulation_principal_arns
      }
    }
  }

  # Control 3 on the restore role (ADR-0046). Its identity policy grants no
  # write here, and the precondition on that policy refuses a recovery bucket
  # as a restore target -- but both live in a file a later change could widen.
  # This does not: a Deny in the resource policy outranks any allow the
  # principal's own policies carry, now or later. A restore that can overwrite
  # its own source is not a restore.
  statement {
    sid    = "RestoreNeverWritesTheArchive"
    effect = "Deny"
    actions = [
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObjectRetention",
      "s3:PutObjectLegalHold",
      "s3:BypassGovernanceRetention",
      "s3:PutBucketPolicy",
      "s3:PutBucketObjectLockConfiguration",
    ]
    resources = [aws_s3_bucket.primary.arn, "${aws_s3_bucket.primary.arn}/*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.restore.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "primary" {
  bucket = aws_s3_bucket.primary.id
  policy = data.aws_iam_policy_document.primary_bucket.json
}

# --- Adding to the archive, from outside AWS -------------------------------------------------
#
# The registry and the source cache are made on a person's machine: `ledger
# build` fetches from the markets' public APIs and is not one of the chains. So
# their way into tier 0 is a person running `aws s3 cp` / `aws s3 sync`, and
# this is the permission to do that and nothing more -- put, under the two
# prefixes; no read, no list of other prefixes, no delete. A credential that
# can only ADD to the archive is one a laptop can hold: stolen, it cannot read
# the labels back out or remove a version (and Object Lock would refuse the
# second anyway). Attached to nobody here: who the archivist is, is the
# account owner's decision.
#
# **The key names the seal** (ADR-0046). Both archives go under a first
# segment that is the sealed manifest's sha256:
#
#     registry/<manifest-sha256>.json
#     source-cache/<manifest-sha256>/<source>/...
#
# which makes "is THIS split archived?" a prefix listing rather than a
# judgement, and makes it impossible for one seal's upload to land on top of
# another's. The DR runbook has prescribed that layout since ADR-0042; a
# runbook sentence is not a mechanism, so the grant now requires it. An upload
# to a key that does not begin with a 64-character segment is refused by IAM,
# not noticed later by a person reading a listing.
data "aws_iam_policy_document" "archive_write" {
  statement {
    sid       = "AddToTheRegistryAndSourceCacheArchivesUnderTheirSeal"
    actions   = ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
    resources = [for prefix in local.label_bearing_prefixes : "${aws_s3_bucket.primary.arn}/${prefix}/${local.sha_glob}*"]
  }
  # `aws s3 sync` lists the destination to decide what to send. Names only,
  # and only under the two prefixes.
  statement {
    sid       = "ListWhatIsAlreadyArchived"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.primary.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [for prefix in local.label_bearing_prefixes : "${prefix}/*"]
    }
  }
  statement {
    sid       = "EncryptThroughS3"
    actions   = ["kms:GenerateDataKey"]
    resources = [var.kms_key_arn]
  }
}

resource "aws_iam_policy" "archive_write" {
  name        = "${var.name}-recovery-archive-write"
  description = "Add objects to the tier-0 registry and source-cache archives: no read, no delete"
  policy      = data.aws_iam_policy_document.archive_write.json
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

# --- Restoring: the role a drill runs as (ADR-0046) ---------------------------------------
#
# Nothing has ever been restored from this bucket. A recovery target that has
# not been exercised is a hope, so `docs/architecture/dr-runbook.md` now
# carries an ordered drill, and this is the identity it runs as.
#
# What it can do: read every tier-0 prefix, in either region, and decrypt.
# That is the whole of the restore path this design documents -- `aws s3 sync`
# of the registry archive and the source cache to a machine, and, for the LLM
# cache, a DataSync task created with the sandbox's own two locations swapped
# (modules/cache exports both ARNs for exactly that).
#
# What it must NOT do, and this is the point: write anything back. **A restore
# that can overwrite its own source is not a restore** -- it is a second way
# to lose the one dataset this project has already lost once. Three
# independent controls say so, and a `terraform test` asserts each:
#
#   1. this policy grants no write action on either bucket;
#   2. `restore_targets.bucket_arns` cannot name them -- the precondition
#      below refuses at plan, before anything exists;
#   3. the primary bucket's own policy Denies this role every mutating call,
#      which outranks any allow a future edit to this policy might add.
#
# Trust is the account root, which is AWS's standard delegation to IAM and the
# same statement this module's replica key policy carries: it grants nobody
# anything by itself, so the role can only be assumed by a principal that has
# been given `sts:AssumeRole` on it explicitly. Who that is, is the account
# owner's decision -- as with the archivist policy above.
data "aws_iam_policy_document" "restore_assume" {
  statement {
    sid     = "TheAccountDelegatesToIam"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
}

resource "aws_iam_role" "restore" {
  name               = "${var.name}-recovery-restore"
  description        = "Read tier 0, in either region, and write only the named restore targets. Never writes the recovery buckets."
  assume_role_policy = data.aws_iam_policy_document.restore_assume.json
}

data "aws_iam_policy_document" "restore" {
  # Both regions: a drill after a region loss restores from the replica, and a
  # role that could only read the primary would be useless in the one scenario
  # cross-region replication was paid for.
  statement {
    sid       = "ReadEveryTierZeroPrefixInBothRegions"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectRetention", "s3:GetObjectLegalHold"]
    resources = ["${aws_s3_bucket.primary.arn}/*", "${aws_s3_bucket.replica.arn}/*"]
  }
  statement {
    sid       = "ListWhatThereIsToRestore"
    actions   = ["s3:ListBucket", "s3:ListBucketVersions", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.primary.arn, aws_s3_bucket.replica.arn]
  }
  # The archive's own listing, so the drill's completeness check reads a
  # report instead of paging through hundreds of thousands of keys.
  statement {
    sid       = "ReadTheArchiveInventory"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [aws_s3_bucket.inventory.arn, "${aws_s3_bucket.inventory.arn}/*"]
  }
  statement {
    sid       = "DecryptWhatItReads"
    actions   = ["kms:Decrypt", "kms:DescribeKey"]
    resources = [var.kms_key_arn, aws_kms_key.replica.arn]
  }

  # The write half, and it is empty unless a target is named. With no target
  # the role is strictly read-only and the drill restores to a machine --
  # which is where this project's one real loss happened (ADR-0042 rejected
  # AWS Backup for the same reason).
  dynamic "statement" {
    for_each = length(var.restore_targets.bucket_arns) > 0 ? [1] : []
    content {
      sid       = "WriteOnlyTheNamedRestoreTargets"
      actions   = ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
      resources = [for arn in var.restore_targets.bucket_arns : "${arn}/*"]
    }
  }
  dynamic "statement" {
    for_each = length(var.restore_targets.bucket_arns) > 0 ? [1] : []
    content {
      sid       = "ListTheNamedRestoreTargets"
      actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
      resources = var.restore_targets.bucket_arns
    }
  }
  dynamic "statement" {
    for_each = length(var.restore_targets.kms_key_arns) > 0 ? [1] : []
    content {
      sid       = "EncryptWhatItRestores"
      actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
      resources = var.restore_targets.kms_key_arns
    }
  }
}

resource "aws_iam_role_policy" "restore" {
  name   = "restore-from-tier-zero"
  role   = aws_iam_role.restore.id
  policy = data.aws_iam_policy_document.restore.json

  lifecycle {
    # Control 2. A variable validation cannot see these names -- they are
    # built from other variables and a data source -- so the refusal lives
    # here, where it still arrives at plan.
    precondition {
      condition = length(setintersection(
        toset(var.restore_targets.bucket_arns),
        toset([aws_s3_bucket.primary.arn, aws_s3_bucket.replica.arn, aws_s3_bucket.inventory.arn]),
      )) == 0
      error_message = "restore_targets.bucket_arns must not name a recovery bucket: a restore that can write its own source is not a restore."
    }
  }
}

# --- What is actually archived, on a schedule (ADR-0046) ----------------------------------
#
# The LLM cache arrives by a scheduled DataSync task. The registry archive and
# the source cache arrive because a person runs `aws s3 sync`, and **that
# cannot be scheduled from here**: `ledger build` runs on a person's machine,
# and the cache that matters is the one the sealed split was derived from, at
# the moment it was derived. Re-deriving one later in AWS on a timer would
# archive responses that correspond to no seal -- worse than no archive,
# because it looks like one. ADR-0046 states the rejected alternatives.
#
# So what is scheduled is the CHECK, not the upload. An S3 Inventory of the
# whole bucket answers, without a `ListObjects` page and without reading a
# single object:
#
#   * is the sealed split's source cache here at all (a `source-cache/<sha>/`
#     row exists), which is the failure a person's manual step actually has;
#   * how many objects each archive holds and how large they are, which is the
#     drill's completeness check -- the DR runbook has listed "a check that
#     the archive's object count matches the file system's entry count" as not
#     built since ADR-0042, and this is it;
#   * that no version has been removed, because the report carries version ids
#     and the Object Lock retain-until-date of each.
#
# An inventory report carries keys, sizes, dates and lock metadata. It never
# carries object CONTENTS, so listing the label-bearing prefixes exports no
# label. The simulation is denied it anyway: it has no reason to read a
# listing of an archive it is denied.

# A bucket of its own, not a fourth prefix in the locked one. Two reasons:
# an inventory is derived and regenerated on the next run, so WORM protects
# nothing and an expiry rule -- which AWS itself recommends for inventories --
# would be a standing attempt to delete locked objects; and delivery by a
# service into a bucket with a default Object Lock retention is exactly the
# class of unknown ADR-0035 hit with AWS Config and ADR-0042 already carries
# once for DataSync. This record declines to carry it twice. The protection
# here is the Config bucket's: versioned, with permanent deletion denied to
# everyone in the bucket policy.
resource "aws_s3_bucket" "inventory" {
  #checkov:skip=CKV_AWS_18:Access logging is CloudTrail data events from the audit module, as for every other bucket in this design; a logging bucket per bucket multiplies what must itself be protected.
  #checkov:skip=CKV_AWS_144:A listing is regenerated on the next schedule; replicating it would pay twice for something the next run reproduces. A region-loss drill counts the replica by listing it.
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events from the inventory bucket; the reports are read by a person or by Athena, on demand.
  bucket = local.inventory_name
}

resource "aws_s3_bucket_versioning" "inventory" {
  bucket = aws_s3_bucket.inventory.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_ownership_controls" "inventory" {
  bucket = aws_s3_bucket.inventory.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "inventory" {
  bucket                  = aws_s3_bucket.inventory.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "inventory" {
  bucket = aws_s3_bucket.inventory.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

# The one bucket here that SHOULD expire its contents: each report supersedes
# the last, and keeping every weekly listing of a growing archive forever
# would cost more than the archive it describes.
resource "aws_s3_bucket_lifecycle_configuration" "inventory" {
  bucket = aws_s3_bucket.inventory.id
  rule {
    id     = "supersede"
    status = "Enabled"
    filter {}
    expiration {
      days = var.inventory_retention_days
    }
    noncurrent_version_expiration {
      noncurrent_days = var.inventory_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

data "aws_iam_policy_document" "inventory_bucket" {
  statement {
    sid       = "TlsOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.inventory.arn, "${aws_s3_bucket.inventory.arn}/*"]
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

  # S3 writes the reports as the service. The two source conditions are what
  # keep another account's inventory configuration from naming this bucket as
  # its destination, which S3 otherwise permits.
  statement {
    sid       = "S3DeliversTheArchiveInventory"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.inventory.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = [aws_s3_bucket.primary.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    # AWS's documented destination policy carries this. With
    # BucketOwnerEnforced ownership, bucket-owner-full-control is the only
    # canned ACL S3 accepts, so requiring it costs nothing and matches the
    # example exactly.
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }

  # What the lock would have done, on a bucket that must stay lifecycle-able.
  # Deliberately NOT a deny on s3:DeleteObject: the lifecycle rule above is
  # carried out by S3 itself and is the one deleter a bucket policy would not
  # bind anyway (the Config bucket's precedent, modules/audit).
  statement {
    sid       = "AListingIsNeverPermanentlyDeletedByHand"
    effect    = "Deny"
    actions   = ["s3:DeleteObjectVersion"]
    resources = ["${aws_s3_bucket.inventory.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }

  # The report names every key under registry/ and source-cache/. A key is not
  # a label -- it is a sha and a file name -- but the simulation has no reason
  # to read a listing of the archive it is denied, and a Deny costs nothing.
  dynamic "statement" {
    for_each = length(var.simulation_principal_arns) > 0 ? [1] : []
    content {
      sid       = "SimulationNeverReadsTheArchiveListing"
      effect    = "Deny"
      actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:ListBucket"]
      resources = [aws_s3_bucket.inventory.arn, "${aws_s3_bucket.inventory.arn}/*"]
      principals {
        type        = "AWS"
        identifiers = var.simulation_principal_arns
      }
    }
  }
}

resource "aws_s3_bucket_policy" "inventory" {
  bucket     = aws_s3_bucket.inventory.id
  policy     = data.aws_iam_policy_document.inventory_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.inventory]
}

resource "aws_s3_bucket_inventory" "archive" {
  bucket = aws_s3_bucket.primary.id
  name   = "tier-0"

  # All versions, not only current. The archive is add-only, so the two counts
  # should agree; a divergence between them is itself the finding, and only
  # the version-aware report can show it.
  included_object_versions = "All"

  schedule {
    # The decision this module refuses to make for the caller: see the
    # variable. Daily or Weekly are the only frequencies S3 offers.
    frequency = var.inventory_schedule
  }

  # Parquet rather than CSV: the platform root already has Athena and a Glue
  # catalog (modules/eventlake), and a columnar report is what a "how many
  # objects under source-cache/<sha>/" query wants. CSV's key names are
  # URL-encoded, which is one decoding step between the operator and the
  # answer during an incident.
  destination {
    bucket {
      format     = "Parquet"
      bucket_arn = aws_s3_bucket.inventory.arn
      account_id = data.aws_caller_identity.current.account_id
      prefix     = "tier-0"
      encryption {
        sse_kms {
          # S3 Inventory does not support the AWS managed aws/s3 key; the
          # platform CMK it is, and its policy must admit s3.amazonaws.com --
          # see `required_key_policy_statements_json`.
          key_id = var.kms_key_arn
        }
      }
    }
  }

  # Enough to answer the drill's questions and nothing that describes who may
  # read what: no ObjectOwner, no ObjectAcl.
  optional_fields = [
    "Size",
    "LastModifiedDate",
    "ETag",
    "StorageClass",
    "IsMultipartUploaded",
    "ReplicationStatus",
    "EncryptionStatus",
    "ObjectLockRetainUntilDate",
    "ObjectLockMode",
  ]

  depends_on = [aws_s3_bucket_policy.inventory]
}

# The statements the caller's key policy must carry for S3 to encrypt the
# inventory reports, in the shape modules/audit established. Built from names,
# the account and the partition only -- never from the key -- so using it in
# that key's own policy is not a cycle.
data "aws_iam_policy_document" "required_key_policy" {
  statement {
    sid       = "S3EncryptsTheArchiveInventory"
    actions   = ["kms:GenerateDataKey"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:s3:::${local.primary_name}"]
    }
  }
}
