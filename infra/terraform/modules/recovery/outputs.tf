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
    archive_write_objects = flatten([for s in data.aws_iam_policy_document.archive_write.statement : s.resources if s.sid == "AddToTheRegistryAndSourceCacheArchives"])
  }
}
