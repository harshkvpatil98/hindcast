output "file_system_id" {
  value = aws_efs_file_system.this.id
}

output "file_system_arn" {
  value = aws_efs_file_system.this.arn
}

output "access_point_id" {
  value = aws_efs_access_point.llm.id
}

output "access_point_arn" {
  description = "A client's IAM grant is conditioned on this: the file system's policy refuses any mount that is not through it."
  value       = aws_efs_access_point.llm.arn
}

output "security_group_id" {
  description = "For a client's own egress rule to the cache, the way modules/bench owns its rule to the database."
  value       = aws_security_group.efs.id
}

output "archive_uri" {
  description = "Where the copy lands. Restore: docs/architecture/dr-runbook.md."
  value       = "s3://${replace(var.recovery_bucket_arn, "/^arn:[^:]+:s3:::/", "")}${local.recovery_subdirectory}"
}

output "sync_task_arn" {
  description = "Start one by hand before destroying a sandbox: `aws datasync start-task-execution --task-arn ...`."
  value       = aws_datasync_task.archive.arn
}

# The two locations, so the restore direction (S3 -> a new file system) is one
# `aws datasync create-task` with them swapped rather than a rebuild from notes.
output "efs_location_arn" {
  value = aws_datasync_location_efs.cache.arn
}

output "s3_location_arn" {
  value = aws_datasync_location_s3.recovery.arn
}

# Read from the resources and the policy documents, not echoed from inputs.
output "controls" {
  value = {
    encrypted                 = aws_efs_file_system.this.encrypted
    kms_key_arn               = aws_efs_file_system.this.kms_key_id
    access_point_uid          = one(aws_efs_access_point.llm.posix_user).uid
    access_point_root         = one(aws_efs_access_point.llm.root_directory).path
    file_system_deny_sids     = [for s in data.aws_iam_policy_document.file_system.statement : s.sid if s.effect == "Deny"]
    sync_task_mode            = aws_datasync_task.archive.task_mode
    sync_schedule             = one(aws_datasync_task.archive.schedule).schedule_expression
    sync_deletes_from_archive = one(aws_datasync_task.archive.options).preserve_deleted_files != "PRESERVE"
    sync_overwrites           = one(aws_datasync_task.archive.options).overwrite_mode != "NEVER"
    sync_excludes             = [for e in aws_datasync_task.archive.excludes : e.value]
    sync_in_transit           = aws_datasync_location_efs.cache.in_transit_encryption
    archive_subdirectory      = aws_datasync_location_s3.recovery.subdirectory
    archive_write_actions     = sort(flatten([for s in data.aws_iam_policy_document.datasync_s3.statement : s.actions if s.sid == "AddEntriesUnderThisPrefixOnly"]))
    archive_write_resources   = flatten([for s in data.aws_iam_policy_document.datasync_s3.statement : s.resources if s.sid == "AddEntriesUnderThisPrefixOnly"])
    sync_reader_actions       = sort(flatten([for s in data.aws_iam_policy_document.datasync_efs.statement : s.actions]))
    sync_failure_rules        = [for r in aws_cloudwatch_event_rule.sync_failed : r.name]
  }
}
