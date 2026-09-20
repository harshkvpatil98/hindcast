output "bucket" {
  value = aws_s3_bucket.primary.bucket
}

output "replica_bucket" {
  value = aws_s3_bucket.replica.bucket
}

output "bucket_arn" {
  value = aws_s3_bucket.primary.arn
}

output "prefixes" {
  description = "The tier-0 prefixes by dataset. Writers take these as inputs; nothing restates them."
  value       = local.prefixes
}

output "source_cache_prefix" {
  description = "Where ledger.source_cache_dir is archived. Holds the labels, inside raw market responses."
  value       = "s3://${aws_s3_bucket.primary.bucket}/${local.prefixes.source_cache}/"
}

output "llm_cache_prefix" {
  description = "Where modules/cache archives llm.cache_dir, one directory per sandbox."
  value       = "s3://${aws_s3_bucket.primary.bucket}/${local.prefixes.llm_cache}/"
}

output "archive_write_policy_arn" {
  description = "Attach to whoever uploads the registry archive and the source cache. Add-only."
  value       = aws_iam_policy.archive_write.arn
}

output "registry_prefix" {
  description = "Where `cascade ledger export` archives go. Holds the labels."
  value       = "s3://${aws_s3_bucket.primary.bucket}/${local.prefixes.registry}/"
}

# Read from the resources, not echoed from the inputs, so a test that asserts
# on this output fails when a resource changes. Exposed because a test run
# cannot load this module directly: `terraform validate` rejects a run whose
# module needs an aliased provider, so it is tested through the root.
output "controls" {
  value = {
    primary_locked           = aws_s3_bucket.primary.object_lock_enabled
    replica_locked           = aws_s3_bucket.replica.object_lock_enabled
    primary_bucket           = aws_s3_bucket.primary.bucket
    replica_bucket           = aws_s3_bucket.replica.bucket
    delete_markers_replicate = one(one(aws_s3_bucket_replication_configuration.this.rule).delete_marker_replication).status
    deny_statements          = [for s in data.aws_iam_policy_document.primary_bucket.statement : s.sid if s.effect == "Deny"]
    labels_deny_actions      = flatten([for s in data.aws_iam_policy_document.primary_bucket.statement : s.actions if s.sid == "SimulationNeverReadsLabels"])
    labels_deny_resources    = flatten([for s in data.aws_iam_policy_document.primary_bucket.statement : s.resources if s.sid == "SimulationNeverReadsLabels"])
    # "" when the rule's filter names no prefix: every object replicates.
    replication_prefix    = try(coalesce(one(one(aws_s3_bucket_replication_configuration.this.rule).filter).prefix, ""), "")
    archive_write_actions = sort(distinct(flatten([for s in data.aws_iam_policy_document.archive_write.statement : s.actions])))
    archive_write_objects = flatten([for s in data.aws_iam_policy_document.archive_write.statement : s.resources if s.sid == "AddToTheRegistryAndSourceCacheArchivesUnderTheirSeal"])

    # --- The restore drill (ADR-0046) ---------------------------------------
    restore_role_arn      = aws_iam_role.restore.arn
    restore_actions       = sort(distinct(flatten([for s in data.aws_iam_policy_document.restore.statement : s.actions])))
    restore_read_buckets  = sort(distinct(flatten([for s in data.aws_iam_policy_document.restore.statement : s.resources if s.sid == "ListWhatThereIsToRestore"])))
    restore_write_targets = sort(distinct(flatten([for s in data.aws_iam_policy_document.restore.statement : s.resources if s.sid == "WriteOnlyTheNamedRestoreTargets"])))
    # Every resource this role may write, by any statement, so a widened
    # policy cannot hide behind a differently named sid.
    restore_written_resources = sort(distinct(flatten([
      for s in data.aws_iam_policy_document.restore.statement : s.resources
      if length([for a in s.actions : a if !startswith(a, "s3:Get") && !startswith(a, "s3:List") && !startswith(a, "kms:De")]) > 0
    ])))
    restore_denied_actions = sort(flatten([
      for s in data.aws_iam_policy_document.primary_bucket.statement : s.actions
      if s.sid == "RestoreNeverWritesTheArchive"
    ]))
    restore_denied_principals = flatten([
      for s in data.aws_iam_policy_document.primary_bucket.statement : [for p in s.principals : tolist(p.identifiers)]
      if s.sid == "RestoreNeverWritesTheArchive"
    ])

    # --- The scheduled archive listing (ADR-0046) ---------------------------
    inventory_bucket     = aws_s3_bucket.inventory.bucket
    inventory_locked     = aws_s3_bucket.inventory.object_lock_enabled
    inventory_source     = aws_s3_bucket_inventory.archive.bucket
    inventory_schedule   = one(aws_s3_bucket_inventory.archive.schedule).frequency
    inventory_versions   = aws_s3_bucket_inventory.archive.included_object_versions
    inventory_format     = one(one(aws_s3_bucket_inventory.archive.destination).bucket).format
    inventory_dest_arn   = one(one(aws_s3_bucket_inventory.archive.destination).bucket).bucket_arn
    inventory_fields     = sort(aws_s3_bucket_inventory.archive.optional_fields)
    inventory_deny_sids  = [for s in data.aws_iam_policy_document.inventory_bucket.statement : s.sid if s.effect == "Deny"]
    inventory_expiry     = one(one(aws_s3_bucket_lifecycle_configuration.inventory.rule).expiration).days
    inventory_versioning = one(aws_s3_bucket_versioning.inventory.versioning_configuration).status
    inventory_allow_source_arns = flatten([
      for s in data.aws_iam_policy_document.inventory_bucket.statement :
      [for c in s.condition : c.values if c.variable == "aws:SourceArn"]
      if s.sid == "S3DeliversTheArchiveInventory"
    ])
    # Without these on the caller's key, S3 cannot encrypt the report and it
    # is simply never delivered -- silence with a green light.
    inventory_key_actions = sort(flatten([for s in data.aws_iam_policy_document.required_key_policy.statement : s.actions]))
    inventory_key_principals = sort(flatten([
      for s in data.aws_iam_policy_document.required_key_policy.statement :
      [for p in s.principals : tolist(p.identifiers)]
    ]))
    inventory_key_source_arns = flatten([
      for s in data.aws_iam_policy_document.required_key_policy.statement :
      [for c in s.condition : c.values if c.variable == "aws:SourceArn"]
    ])
  }
}

output "restore_role_arn" {
  description = "Assume this to run the drill in docs/architecture/dr-runbook.md. Reads tier 0 in both regions; writes nothing here, by three independent controls."
  value       = aws_iam_role.restore.arn
}

output "inventory_bucket" {
  description = "Where the scheduled listing of the tier-0 archive is delivered. Keys, sizes and lock metadata; never object contents."
  value       = aws_s3_bucket.inventory.bucket
}

output "inventory_uri" {
  description = "Query it with Athena, or fetch the newest manifest: `aws s3 ls <uri> --recursive`."
  value       = "s3://${aws_s3_bucket.inventory.bucket}/tier-0/"
}

output "required_key_policy_statements_json" {
  description = <<-EOT
    Statements the caller's KMS key policy must carry, or S3 cannot encrypt
    the inventory report and it is simply never delivered. Merge with
    `source_policy_documents`, the way modules/audit's are merged. Depends on
    names, the account and the partition only, never on the key, so using it
    in that key's own policy is not a cycle.
  EOT
  value       = data.aws_iam_policy_document.required_key_policy.json
}
