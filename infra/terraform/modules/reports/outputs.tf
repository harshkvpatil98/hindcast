output "bucket" {
  value = aws_s3_bucket.this.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.this.arn
}

output "prefix" {
  description = "The one prefix reports are written under. Writers take this as an input; nothing restates it."
  value       = local.prefix
}

output "uri" {
  description = "Where a reader syncs from: `aws s3 sync <uri><study_id>/ ./study/` (docs/architecture/dr-runbook.md)."
  value       = "s3://${aws_s3_bucket.this.bucket}/${local.prefix}/"
}

output "read_policy_arn" {
  description = "Attach to whoever may read a published report. Read-only: no write, no delete, no lock override."
  value       = aws_iam_policy.read.arn
}

# Feed this whole object to the sandbox's `study.reports`: which bucket, under
# which key, under which prefix -- passed, so the two roots cannot spell the
# prefix differently.
output "publish" {
  value = {
    bucket_arn  = aws_s3_bucket.this.arn
    kms_key_arn = var.kms_key_arn
    prefix      = local.prefix
  }
}

# Read from the resources and the policy documents, not echoed from the
# inputs, so a test that asserts on this output fails when a resource changes
# -- including from the root, where the module's resources are out of a test's
# reach.
output "controls" {
  value = {
    bucket           = aws_s3_bucket.this.bucket
    locked           = aws_s3_bucket.this.object_lock_enabled
    retention_mode   = one(one(aws_s3_bucket_object_lock_configuration.this.rule).default_retention).mode
    retention_days   = one(one(aws_s3_bucket_object_lock_configuration.this.rule).default_retention).days
    versioning       = one(aws_s3_bucket_versioning.this.versioning_configuration).status
    encryption_key   = one(one(aws_s3_bucket_server_side_encryption_configuration.this.rule).apply_server_side_encryption_by_default).kms_master_key_id
    object_ownership = one(aws_s3_bucket_ownership_controls.this.rule).object_ownership
    public_blocks = [
      aws_s3_bucket_public_access_block.this.block_public_acls,
      aws_s3_bucket_public_access_block.this.block_public_policy,
      aws_s3_bucket_public_access_block.this.ignore_public_acls,
      aws_s3_bucket_public_access_block.this.restrict_public_buckets,
    ]
    deny_statements = [for s in data.aws_iam_policy_document.bucket.statement : s.sid if s.effect == "Deny"]
    immutability_deny_actions = sort(flatten([
      for s in data.aws_iam_policy_document.bucket.statement : s.actions
      if s.sid == "NoVersionIsEverRemovedAndNoLockIsEverLifted"
    ]))
    labels_deny_actions = flatten([
      for s in data.aws_iam_policy_document.bucket.statement : s.actions
      if s.sid == "SimulationNeverReadsReports"
    ])
    labels_deny_resources = flatten([
      for s in data.aws_iam_policy_document.bucket.statement : s.resources
      if s.sid == "SimulationNeverReadsReports"
    ])
    labels_deny_principals = flatten([
      for s in data.aws_iam_policy_document.bucket.statement : [for p in s.principals : tolist(p.identifiers)]
      if s.sid == "SimulationNeverReadsReports"
    ])
    # Every principal that could serve this bucket to an anonymous viewer
    # would have to appear here. It stays empty: CloudFront's service
    # principal is not admitted, because there is no distribution (ADR-0046).
    allow_service_principals = sort(distinct(flatten([
      for s in data.aws_iam_policy_document.bucket.statement :
      [for p in s.principals : tolist(p.identifiers) if p.type == "Service"]
      if s.effect != "Deny"
    ])))
    read_policy_actions = sort(distinct(flatten([for s in data.aws_iam_policy_document.read.statement : s.actions])))
    read_policy_objects = flatten([for s in data.aws_iam_policy_document.read.statement : s.resources if s.sid == "ReadPublishedReports"])
  }
}
