output "events_bucket" {
  value = aws_s3_bucket.events.bucket
}

output "workgroup" {
  value = aws_athena_workgroup.this.name
}

output "writer_role_arn" {
  value = aws_iam_role.writer.arn
}

output "analyst_role_arn" {
  value = aws_iam_role.analyst.arn
}

output "writer_policy_json" {
  value = data.aws_iam_policy_document.writer.json
}

# Read from the bucket, not echoed from the inputs.
output "object_lock" {
  description = "Whether the events bucket was created with Object Lock."
  value       = aws_s3_bucket.events.object_lock_enabled
}

output "force_destroy" {
  description = "Whether a destroy may empty the events bucket."
  value       = aws_s3_bucket.events.force_destroy
}
