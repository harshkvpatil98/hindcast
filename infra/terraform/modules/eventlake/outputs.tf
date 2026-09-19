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
