output "task_definition_arn" {
  value = aws_ecs_task_definition.study.arn
}

output "task_role_arn" {
  description = <<-EOT
    What the study's phases run as. This is a simulation principal: give it to
    the platform root's `simulation_principal_arns`, which denies it the
    registry archive and the source cache in the recovery bucket (invariant 2).
  EOT
  value       = aws_iam_role.task.arn
}

output "security_group_id" {
  value = aws_security_group.task.id
}

# Read from what the resources and policy documents carry.
output "controls" {
  value = {
    environment       = { for e in jsondecode(aws_ecs_task_definition.study.container_definitions)[0].environment : e.name => e.value }
    secret_names      = [for s in jsondecode(aws_ecs_task_definition.study.container_definitions)[0].secrets : s.name]
    mount_points      = { for m in jsondecode(aws_ecs_task_definition.study.container_definitions)[0].mountPoints : m.containerPath => m.sourceVolume }
    read_only_root    = jsondecode(aws_ecs_task_definition.study.container_definitions)[0].readonlyRootFilesystem
    efs_volumes       = [for v in aws_ecs_task_definition.study.volume : { name = v.name, transit = one(v.efs_volume_configuration).transit_encryption, iam = one(one(v.efs_volume_configuration).authorization_config).iam } if length(v.efs_volume_configuration) > 0]
    model_actions     = sort(flatten([for s in data.aws_iam_policy_document.task.statement : s.actions if startswith(s.sid, "Call")]))
    model_resources   = flatten([for s in data.aws_iam_policy_document.task.statement : s.resources if startswith(s.sid, "Call")])
    wildcard_services = sort(distinct(flatten([for s in data.aws_iam_policy_document.task.statement : [for a in s.actions : split(":", a)[0]] if contains(s.resources, "*")])))
    cache_actions     = sort(flatten([for s in data.aws_iam_policy_document.task.statement : s.actions if s.sid == "ReadAndWriteTheCacheThroughItsAccessPoint"]))
    cache_conditions  = flatten([for s in data.aws_iam_policy_document.task.statement : [for c in s.condition : c.values if c.variable == "elasticfilesystem:AccessPointArn"] if s.sid == "ReadAndWriteTheCacheThroughItsAccessPoint"])
  }
}
