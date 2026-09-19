output "cluster_endpoint" {
  value = module.database.endpoint
}

output "clone_endpoint" {
  value = module.database.clone_endpoint
}

output "repository_url" {
  description = "Push the bench image here: see infra/terraform/README.md."
  value       = module.bench.repository_url
}

output "artifacts_bucket" {
  value = module.bench.artifacts_bucket
}

output "log_group" {
  value = module.bench.log_group
}

# The exact command to run any cascade subcommand inside the VPC. Printed
# rather than documented so it cannot drift from the resources it names.
output "run_task" {
  description = "Append the subcommand as a JSON array, e.g. '[\"cascade\",\"retrieval\",\"bench\"]'."
  value = join(" ", [
    "aws ecs run-task --region ${var.region}",
    "--cluster ${module.bench.cluster_name}",
    "--task-definition ${module.bench.task_definition_arn}",
    "--launch-type FARGATE",
    "--network-configuration 'awsvpcConfiguration={subnets=[${join(",", module.network.private_subnet_ids)}],securityGroups=[${module.bench.security_group_id}],assignPublicIp=DISABLED}'",
    "--overrides '{\"containerOverrides\":[{\"name\":\"cascade\",\"command\":COMMAND}]}'",
  ])
}
