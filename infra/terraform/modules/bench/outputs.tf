output "security_group_id" {
  value = aws_security_group.task.id
}

output "repository_url" {
  value = aws_ecr_repository.bench.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.bench.arn
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "log_group" {
  value = aws_cloudwatch_log_group.task.name
}
