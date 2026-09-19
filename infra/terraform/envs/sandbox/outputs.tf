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

output "egress" {
  description = "Null unless the egress tier was asked for."
  value = var.egress == null ? null : {
    subnet_ids        = module.egress[0].subnet_ids
    security_group_id = module.egress[0].security_group_id
    nat_public_ip     = module.egress[0].nat_public_ip
    dns_log_group     = module.egress[0].dns_log_group
  }
}

output "study" {
  description = <<-EOT
    Null unless the study task was asked for. `task_role_arn` is a simulation
    principal: list it in the platform root's `simulation_principal_arns`.
    Start `sync_task_arn` by hand before `terraform destroy` -- the file system
    goes with the sandbox, and the archive is only as new as the last run.
  EOT
  value = var.study == null ? null : {
    task_definition_arn = module.study[0].task_definition_arn
    task_role_arn       = module.study[0].task_role_arn
    security_group_id   = module.study[0].security_group_id
    llm_cache_archive   = module.cache[0].archive_uri
    sync_task_arn       = module.cache[0].sync_task_arn
    efs_location_arn    = module.cache[0].efs_location_arn
    s3_location_arn     = module.cache[0].s3_location_arn
  }
}
