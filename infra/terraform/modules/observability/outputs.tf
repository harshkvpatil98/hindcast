output "dashboard_name" {
  value = aws_cloudwatch_dashboard.this.dashboard_name
}

output "event_rule_arn" {
  description = "The rule that alerts on a non-zero container exit code."
  value       = aws_cloudwatch_event_rule.task_exited.arn
}

output "never_started_rule_arn" {
  description = "The rule that alerts on a task that stopped before any container ran."
  value       = aws_cloudwatch_event_rule.task_never_started.arn
}

# The two documents below are requirements, not resources. The topic and its
# key are created elsewhere and each can carry one policy; merge these into
# them (`source_policy_documents`). Until both are, nothing here can alert.
output "alerts_topic_policy_json" {
  description = "Statements the alerts topic's policy must carry: events.amazonaws.com for the two rules (aws:SourceArn), cloudwatch.amazonaws.com for the alarms."
  value       = data.aws_iam_policy_document.alerts_topic.json
}

output "alerts_key_policy_json" {
  description = "Statements the alerts topic's KMS key policy must carry. The EventBridge statement has no source condition on purpose: AWS does not support one there."
  value       = data.aws_iam_policy_document.alerts_key.json
}

# Read from the resources, not echoed from the inputs, so a test that asserts
# on this output fails when a resource changes.
output "controls" {
  value = {
    task_exited_pattern        = jsondecode(aws_cloudwatch_event_rule.task_exited.event_pattern)
    task_exited_input_paths    = one(aws_cloudwatch_event_target.task_exited.input_transformer).input_paths
    task_exited_template       = one(aws_cloudwatch_event_target.task_exited.input_transformer).input_template
    task_never_started_pattern = jsondecode(aws_cloudwatch_event_rule.task_never_started.event_pattern)
    alert_targets = toset([
      aws_cloudwatch_event_target.task_exited.arn,
      aws_cloudwatch_event_target.task_never_started.arn,
    ])
    traceback_filter = {
      log_group = aws_cloudwatch_log_metric_filter.traceback.log_group_name
      pattern   = aws_cloudwatch_log_metric_filter.traceback.pattern
      metric    = "${one(aws_cloudwatch_log_metric_filter.traceback.metric_transformation).namespace}/${one(aws_cloudwatch_log_metric_filter.traceback.metric_transformation).name}"
    }
    alarms = { for key, alarm in aws_cloudwatch_metric_alarm.this : key => {
      metric             = "${alarm.namespace}/${alarm.metric_name}"
      dimensions         = alarm.dimensions
      comparison         = alarm.comparison_operator
      threshold          = alarm.threshold
      alarm_actions      = alarm.alarm_actions
      ok_actions         = alarm.ok_actions
      treat_missing_data = alarm.treat_missing_data
    } }
    dashboard_metrics = distinct(flatten([
      for widget in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets :
      [for metric in widget.properties.metrics : "${metric[0]}/${metric[1]}"]
      if widget.type == "metric"
    ]))
  }
}
