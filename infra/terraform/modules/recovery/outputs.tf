output "bucket" {
  value = aws_s3_bucket.primary.bucket
}

output "replica_bucket" {
  value = aws_s3_bucket.replica.bucket
}

output "registry_prefix" {
  description = "Where `cascade ledger export` archives go. Holds the labels."
  value       = "s3://${aws_s3_bucket.primary.bucket}/registry/"
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
  }
}
