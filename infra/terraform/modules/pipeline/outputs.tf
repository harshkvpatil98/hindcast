output "ingest_state_machine_arn" {
  value = aws_sfn_state_machine.this["ingest"].arn
}

output "study_state_machine_arn" {
  value = aws_sfn_state_machine.this["study"].arn
}

output "role_arn" {
  value = aws_iam_role.states.arn
}

output "log_group" {
  value = aws_cloudwatch_log_group.states.name
}

# Decoded from the definitions the resources carry, not echoed from the locals
# that built them, so an assertion on this output fails when a machine changes.
# `next` and `commands` together are the chain: a test walks them from
# `start_at`.
output "controls" {
  value = {
    for machine, resource in aws_sfn_state_machine.this : machine => {
      start_at = jsondecode(resource.definition).StartAt
      next     = { for name, state in jsondecode(resource.definition).States : name => state.Next if can(state.Next) }
      commands = {
        for name, state in jsondecode(resource.definition).States :
        name => join(" ", one(state.Parameters.Overrides.ContainerOverrides).Command)
        if can(state.Parameters.Overrides)
      }
      uncaught_tasks = sort([
        for name, state in jsondecode(resource.definition).States : name
        if state.Type == "Task" && !contains(flatten([for catch in try(state.Catch, []) : catch.ErrorEquals]), "States.ALL")
      ])
      reruns_failed_tasks = sort([
        for name, state in jsondecode(resource.definition).States : name
        if length(setintersection(flatten([for retry in try(state.Retry, []) : retry.ErrorEquals]), ["States.ALL", "States.TaskFailed", "States.Timeout"])) > 0 && can(state.Parameters.Overrides)
      ])
      public_ip_settings = distinct([
        for name, state in jsondecode(resource.definition).States :
        state.Parameters.NetworkConfiguration.AwsvpcConfiguration.AssignPublicIp
        if can(state.Parameters.NetworkConfiguration)
      ])
      state_types      = distinct(sort([for name, state in jsondecode(resource.definition).States : state.Type]))
      logging_level    = one(resource.logging_configuration).level
      logs_payloads    = one(resource.logging_configuration).include_execution_data
      tracing          = one(resource.tracing_configuration).enabled
      failure_terminal = jsondecode(resource.definition).States[jsondecode(resource.definition).States.NotifyFailure.Next].Type
    }
  }
}
