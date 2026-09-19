output "security_group_id" {
  value = aws_security_group.task.id
}

output "repository_url" {
  value = aws_ecr_repository.bench.repository_url
}

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  description = "EventBridge matches ECS events on the cluster's ARN; its name matches nothing."
  value       = aws_ecs_cluster.this.arn
}

output "execution_role_arn" {
  description = "For an orchestrator's iam:PassRole: what ECS itself assumes to start the task."
  value       = aws_iam_role.execution.arn
}

output "task_role_arn" {
  description = "For an orchestrator's iam:PassRole: what the bench code runs as."
  value       = aws_iam_role.task.arn
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
