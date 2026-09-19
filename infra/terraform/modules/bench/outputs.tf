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

# --- For modules/study: the same task, with a model and a durable cache ------------

output "container_definition" {
  description = "The bench container as an object. modules/study overrides a few keys and restates none. Holds secret ARNs, never secret values."
  value       = local.container
}

output "execution_role_name" {
  description = "So a caller can let ECS resolve one more secret (a model API key) without this module knowing models exist."
  value       = aws_iam_role.execution.name
}

output "db_user_arns" {
  description = "The rds-db:connect resources for the application roles, for any other task role that logs in with IAM tokens."
  value       = local.db_users
}

output "task_cpu" {
  value = aws_ecs_task_definition.bench.cpu
}

output "task_memory" {
  value = aws_ecs_task_definition.bench.memory
}
