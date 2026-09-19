output "cluster_identifier" {
  value = aws_rds_cluster.this.cluster_identifier
}

output "endpoint" {
  value = aws_rds_cluster.this.endpoint
}

output "clone_endpoint" {
  value = var.experiment_clone ? aws_rds_cluster.clone[0].endpoint : null
}

output "port" {
  value = aws_rds_cluster.this.port
}

output "cluster_resource_id" {
  description = "For rds-db:connect ARNs (IAM database authentication)."
  value       = aws_rds_cluster.this.cluster_resource_id
}

output "security_group_id" {
  value = aws_security_group.db.id
}

output "kms_key_arn" {
  value = aws_kms_key.data.arn
}

output "master_user_secret_arn" {
  value = aws_rds_cluster.this.master_user_secret[0].secret_arn
}

output "role_secret_arns" {
  value = { for role, secret in aws_secretsmanager_secret.role : role => secret.arn }
}
