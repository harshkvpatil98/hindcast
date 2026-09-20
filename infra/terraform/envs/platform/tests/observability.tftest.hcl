# Observability, offline: a stopped task alerts with its exit code intact, every
# alarm reaches the alerts topic, and no threshold is invented.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_rds_cluster" {
    defaults = {
      arn                = "arn:aws:rds:us-east-1:123456789012:cluster:mock"
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:master", kms_key_id = "k", secret_status = "active" }]
    }
  }
  # Computed ARNs that flow into ARN-typed arguments must look like ARNs: the
  # provider validates them even under mocks, which is worth keeping.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/mock", key_id = "mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/mock", repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mock" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_ce_anomaly_monitor" {
    defaults = { arn = "arn:aws:ce::123456789012:anomalymonitor/mock" }
  }
  mock_resource "aws_athena_workgroup" {
    defaults = { arn = "arn:aws:athena:us-east-1:123456789012:workgroup/mock" }
  }
  mock_resource "aws_organizations_policy" {
    defaults = { id = "p-mock1234" }
  }
}

# The root module declares aws.replica; a test file that mocks any provider
# replaces them all, so every file in this root must supply both.
mock_provider "aws" {
  alias = "replica"
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_rds_cluster" {
    defaults = {
      arn                = "arn:aws:rds:us-east-1:123456789012:cluster:mock"
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:master", kms_key_id = "k", secret_status = "active" }]
    }
  }
  # Computed ARNs that flow into ARN-typed arguments must look like ARNs: the
  # provider validates them even under mocks, which is worth keeping.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/mock", key_id = "mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/mock", repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mock" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_ce_anomaly_monitor" {
    defaults = { arn = "arn:aws:ce::123456789012:anomalymonitor/mock" }
  }
  mock_resource "aws_athena_workgroup" {
    defaults = { arn = "arn:aws:athena:us-east-1:123456789012:workgroup/mock" }
  }
  mock_resource "aws_organizations_policy" {
    defaults = { id = "p-mock1234" }
  }
}

# No mock ARN is given for aws_cloudwatch_event_rule or aws_cloudwatch_metric_alarm
# on purpose. Neither flows into an ARN-typed argument (a target names its rule,
# and an alarm's actions are the topic), and a shared default would make the two
# rules' ARNs identical -- the policy test below could not then tell one from both.

variables {
  name                  = "t"
  alerts_topic_arn      = "arn:aws:sns:us-east-1:123456789012:t-cost-alerts"
  cluster_arn           = "arn:aws:ecs:us-east-1:123456789012:cluster/t-bench"
  db_cluster_identifier = "t-db"
  task_log_group_name   = "/cascade/t/task"
  # A cluster that can scale, at a ceiling no default anywhere would produce:
  # an alarm threshold of 13.5 can only have come from this input.
  min_acu = 0.5
  max_acu = 13.5
  # Test inputs, not recommendations: the module refuses to supply either.
  freeable_memory_low_bytes = 1610612736
  database_connections_high = 77
}

run "a_stopped_task_alerts_only_on_a_nonzero_exit_code" {
  command = plan
  module {
    source = "../../modules/observability"
  }

  assert {
    condition = (
      jsondecode(aws_cloudwatch_event_rule.task_exited.event_pattern).source == ["aws.ecs"] &&
      jsondecode(aws_cloudwatch_event_rule.task_exited.event_pattern)["detail-type"] == ["ECS Task State Change"]
    )
    error_message = "The rule must listen to ECS task state changes."
  }
  assert {
    condition     = jsondecode(aws_cloudwatch_event_rule.task_exited.event_pattern).detail.lastStatus == ["STOPPED"]
    error_message = "Only a STOPPED task has an exit code worth alerting on."
  }
  assert {
    condition     = jsondecode(aws_cloudwatch_event_rule.task_exited.event_pattern).detail.clusterArn == [var.cluster_arn]
    error_message = "The rule must be confined to this cluster, by ARN: events carry clusterArn, never a name."
  }
  # The whole clause, not a substring: `exists`, a numeric range admitting 0, or
  # a second alternative beside anything-but would each let a clean exit alert.
  assert {
    condition     = jsonencode(jsondecode(aws_cloudwatch_event_rule.task_exited.event_pattern).detail.containers) == jsonencode({ exitCode = [{ "anything-but" = [0] }] })
    error_message = "The exit-code clause must be exactly anything-but [0]: exit 0 is not a failure."
  }
  assert {
    condition     = toset(keys(jsondecode(aws_cloudwatch_event_rule.task_exited.event_pattern).detail)) == toset(["clusterArn", "containers", "lastStatus"])
    error_message = "No other clause: an $or beside the exit-code clause could match a clean exit."
  }
}

run "a_task_that_never_started_is_not_silent" {
  command = plan
  module {
    source = "../../modules/observability"
  }

  # No container ran, so there is no exit code for anything-but to match.
  assert {
    condition = jsonencode(jsondecode(aws_cloudwatch_event_rule.task_never_started.event_pattern).detail) == jsonencode({
      clusterArn = [var.cluster_arn]
      lastStatus = ["STOPPED"]
      stopCode   = ["TaskFailedToStart"]
    })
    error_message = "A task that failed to start must alert too, on this cluster only."
  }
  assert {
    condition     = strcontains(one(aws_cloudwatch_event_target.task_never_started.input_transformer).input_template, "<stopped_reason>")
    error_message = "With no exit code, the stopped reason is the whole diagnosis."
  }
}

run "the_alert_carries_the_exit_code_and_the_reason" {
  command = plan
  module {
    source = "../../modules/observability"
  }

  assert {
    condition = (
      aws_cloudwatch_event_target.task_exited.arn == var.alerts_topic_arn &&
      aws_cloudwatch_event_target.task_never_started.arn == var.alerts_topic_arn &&
      aws_cloudwatch_event_target.task_exited.rule == aws_cloudwatch_event_rule.task_exited.name &&
      aws_cloudwatch_event_target.task_never_started.rule == aws_cloudwatch_event_rule.task_never_started.name
    )
    error_message = "Each rule must target the alerts topic."
  }
  assert {
    condition = (
      one(aws_cloudwatch_event_target.task_exited.input_transformer).input_paths["exit_code"] == "$.detail.containers[0].exitCode" &&
      one(aws_cloudwatch_event_target.task_exited.input_transformer).input_paths["stopped_reason"] == "$.detail.stoppedReason" &&
      one(aws_cloudwatch_event_target.task_exited.input_transformer).input_paths["task_arn"] == "$.detail.taskArn"
    )
    error_message = "The transformer must read the exit code, the stopped reason and the task from the event."
  }
  assert {
    condition = alltrue([
      for placeholder in ["<exit_code>", "<stopped_reason>", "<task_arn>"] :
      strcontains(one(aws_cloudwatch_event_target.task_exited.input_transformer).input_template, placeholder)
    ])
    error_message = "An alert that says 'task failed' without the exit code cannot tell a budget stop (2) from a bug (1)."
  }
  # A placeholder with no path renders as nothing, silently.
  assert {
    condition = alltrue([
      for target in [aws_cloudwatch_event_target.task_exited, aws_cloudwatch_event_target.task_never_started] :
      length(setsubtract(
        flatten(regexall("<([a-z_]+)>", one(target.input_transformer).input_template)),
        keys(one(target.input_transformer).input_paths)
      )) == 0
    ])
    error_message = "Every placeholder in a template must have an input path."
  }
  # The exit-code contract, CLAUDE.md section 4: all four, each on its own line.
  assert {
    condition = alltrue([
      for line in ["  1 = unexpected error", "  2 = budget ceiling breached", "  3 = precondition failed", "  4 = cache miss in replay"] :
      strcontains(one(aws_cloudwatch_event_target.task_exited.input_transformer).input_template, "\"${line}")
    ])
    error_message = "The alert must say what each of this project's exit codes means."
  }
  # EventBridge's text form: every line its own quoted string, no stray quote.
  assert {
    condition = alltrue([
      for line in split("\n", one(aws_cloudwatch_event_target.task_exited.input_transformer).input_template) :
      can(regex("^\"[^\"]+\"$", line))
    ])
    error_message = "Each template line must be one double-quoted string."
  }
}

run "every_alarm_reaches_the_alerts_topic_in_both_directions" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  variables {
    state_machine_arns = ["arn:aws:states:us-east-1:123456789012:stateMachine:t-ingest"]
  }

  # Guard the guard: alltrue over nothing is true.
  assert {
    condition = toset(keys(aws_cloudwatch_metric_alarm.this)) == toset([
      "alert-delivery-task-exited", "alert-delivery-task-never-started",
      "aurora-acu-utilization-high", "aurora-capacity-at-max", "aurora-connections-high",
      "aurora-cpu-high", "aurora-freeable-memory-low", "task-traceback",
      "sfn-0-ExecutionsAborted", "sfn-0-ExecutionsFailed", "sfn-0-ExecutionsTimedOut",
    ])
    error_message = "The alarm set changed: update this list, and check the new alarm below."
  }
  assert {
    condition     = alltrue([for alarm in aws_cloudwatch_metric_alarm.this : alarm.alarm_actions == toset([var.alerts_topic_arn])])
    error_message = "Every alarm must notify the alerts topic: ${join(", ", [for key, alarm in aws_cloudwatch_metric_alarm.this : key if alarm.alarm_actions != toset([var.alerts_topic_arn])])}"
  }
  assert {
    condition     = alltrue([for alarm in aws_cloudwatch_metric_alarm.this : alarm.ok_actions == toset([var.alerts_topic_arn])])
    error_message = "Every alarm must say when it clears: ${join(", ", [for key, alarm in aws_cloudwatch_metric_alarm.this : key if alarm.ok_actions != toset([var.alerts_topic_arn])])}"
  }
  # `!= false`: the provider's default (true) is not applied under mocks, so
  # unset reads as null here. Only an explicit false is the defect.
  assert {
    condition     = alltrue([for alarm in aws_cloudwatch_metric_alarm.this : alarm.actions_enabled != false])
    error_message = "An alarm with its actions disabled notifies nobody."
  }
  # Every metric here exists only while something runs, or only when the bad
  # thing happens. A liveness alarm would need the opposite and is not in this set.
  assert {
    condition     = alltrue([for alarm in aws_cloudwatch_metric_alarm.this : alarm.treat_missing_data == "notBreaching"])
    error_message = "In an environment that is created, measured and destroyed, a missing metric is the normal state."
  }
  assert {
    condition     = output.controls.alarms["aurora-cpu-high"].alarm_actions == toset([var.alerts_topic_arn])
    error_message = "The controls output must read the alarms back from the resources."
  }
}

run "the_capacity_alarm_threshold_is_the_supplied_max_acu" {
  command = plan
  module {
    source = "../../modules/observability"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.this["aurora-capacity-at-max"].threshold == 13.5
    error_message = "The capacity threshold must be max_acu, read from the caller."
  }
  assert {
    condition = (
      aws_cloudwatch_metric_alarm.this["aurora-capacity-at-max"].namespace == "AWS/RDS" &&
      aws_cloudwatch_metric_alarm.this["aurora-capacity-at-max"].metric_name == "ServerlessDatabaseCapacity" &&
      aws_cloudwatch_metric_alarm.this["aurora-capacity-at-max"].comparison_operator == "GreaterThanOrEqualToThreshold" &&
      aws_cloudwatch_metric_alarm.this["aurora-capacity-at-max"].dimensions == tomap({ DBClusterIdentifier = "t-db" })
    )
    error_message = "Capacity at or above the ceiling, on this cluster."
  }
  assert {
    condition     = strcontains(aws_cloudwatch_metric_alarm.this["aurora-capacity-at-max"].alarm_description, "capacity-bound")
    error_message = "The description must say what sustained maximum capacity means for a measurement."
  }
  assert {
    condition = (
      aws_cloudwatch_metric_alarm.this["aurora-freeable-memory-low"].threshold == 1610612736 &&
      aws_cloudwatch_metric_alarm.this["aurora-freeable-memory-low"].comparison_operator == "LessThanThreshold" &&
      aws_cloudwatch_metric_alarm.this["aurora-connections-high"].threshold == 77 &&
      aws_cloudwatch_metric_alarm.this["aurora-connections-high"].comparison_operator == "GreaterThanOrEqualToThreshold"
    )
    error_message = "Memory alarms low and connections alarm high, at the caller's thresholds."
  }
  assert {
    condition = alltrue([
      for key in ["aurora-acu-utilization-high", "aurora-cpu-high", "aurora-freeable-memory-low", "aurora-connections-high"] :
      aws_cloudwatch_metric_alarm.this[key].dimensions == tomap({ DBClusterIdentifier = "t-db" })
    ])
    error_message = "Every Aurora alarm must watch the named cluster."
  }
}

run "the_capacity_alarm_follows_a_different_ceiling" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  variables {
    max_acu = 21.5
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.this["aurora-capacity-at-max"].threshold == 21.5
    error_message = "With max_acu = 21.5 the threshold is 21.5: it must be read, not restated."
  }
}

run "a_pinned_cluster_gets_no_alarm_that_would_fire_forever" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  # envs/sandbox: min_acu = max_acu = var.acu.
  variables {
    min_acu = 8
    max_acu = 8
  }

  assert {
    condition = (
      !contains(keys(aws_cloudwatch_metric_alarm.this), "aurora-capacity-at-max") &&
      !contains(keys(aws_cloudwatch_metric_alarm.this), "aurora-acu-utilization-high")
    )
    error_message = "With capacity pinned, capacity = max_acu and ACUUtilization = 100 for the cluster's whole life: those alarms would be permanent noise."
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.this["aurora-cpu-high"].metric_name == "CPUUtilization"
    error_message = "A pinned cluster still needs a capacity-bound signal: CPU against what max_acu provides."
  }
}

run "a_traceback_in_the_task_log_alarms" {
  command = plan
  module {
    source = "../../modules/observability"
  }

  assert {
    condition = (
      aws_cloudwatch_log_metric_filter.traceback.log_group_name == "/cascade/t/task" &&
      aws_cloudwatch_log_metric_filter.traceback.pattern == "Traceback"
    )
    error_message = "The filter must count Traceback lines in the task's log group."
  }
  # An alarm on a metric no filter emits applies cleanly and never fires.
  assert {
    condition = (
      aws_cloudwatch_metric_alarm.this["task-traceback"].namespace == one(aws_cloudwatch_log_metric_filter.traceback.metric_transformation).namespace &&
      aws_cloudwatch_metric_alarm.this["task-traceback"].metric_name == one(aws_cloudwatch_log_metric_filter.traceback.metric_transformation).name
    )
    error_message = "The alarm must watch exactly the metric the filter emits."
  }
  assert {
    condition = (
      aws_cloudwatch_metric_alarm.this["task-traceback"].statistic == "Sum" &&
      aws_cloudwatch_metric_alarm.this["task-traceback"].threshold == 1 &&
      aws_cloudwatch_metric_alarm.this["task-traceback"].evaluation_periods == 1
    )
    error_message = "One traceback is one bug: no debounce."
  }
}

run "one_step_functions_alarm_per_machine_and_metric" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  variables {
    state_machine_arns = [
      "arn:aws:states:us-east-1:123456789012:stateMachine:t-ingest",
      "arn:aws:states:us-east-1:123456789012:stateMachine:t-simulate",
    ]
  }

  assert {
    condition = toset([
      for alarm in aws_cloudwatch_metric_alarm.this : "${alarm.dimensions["StateMachineArn"]}|${alarm.metric_name}"
      if alarm.namespace == "AWS/States"
      ]) == toset([
      for pair in setproduct(var.state_machine_arns, ["ExecutionsAborted", "ExecutionsFailed", "ExecutionsTimedOut"]) : "${pair[0]}|${pair[1]}"
    ])
    error_message = "One alarm per (state machine, failure metric)."
  }
  assert {
    condition     = length([for alarm in aws_cloudwatch_metric_alarm.this : alarm if alarm.namespace == "AWS/States"]) == 6
    error_message = "Two machines, three failure metrics, six alarms."
  }
  assert {
    condition = toset([
      for metric in one([for widget in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : widget if widget.type == "metric" && widget.properties.title == "Step Functions executions"]).properties.metrics :
      "${metric[3]}|${metric[1]}"
      ]) == toset([
      for pair in setproduct(var.state_machine_arns, ["ExecutionsStarted", "ExecutionsSucceeded", "ExecutionsAborted", "ExecutionsFailed", "ExecutionsTimedOut"]) : "${pair[0]}|${pair[1]}"
    ])
    error_message = "The dashboard must chart every machine's executions."
  }
}

run "no_state_machines_means_no_step_functions_alarms" {
  command = plan
  module {
    source = "../../modules/observability"
  }

  assert {
    condition     = length([for alarm in aws_cloudwatch_metric_alarm.this : alarm if alarm.namespace == "AWS/States"]) == 0
    error_message = "The default is no state machines, and so no Step Functions alarms."
  }
  # PutDashboard refuses a metric widget with no metrics.
  assert {
    condition = alltrue([
      for widget in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets :
      length(widget.properties.metrics) > 0 if widget.type == "metric"
    ])
    error_message = "No metric widget may be empty."
  }
  assert {
    condition     = length([for widget in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : widget if widget.type == "text"]) == 1
    error_message = "With nothing to chart, the Step Functions row must say why."
  }
}

run "the_dashboard_charts_what_the_alarms_watch" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  # A region no literal in the module could match by accident.
  override_data {
    target = data.aws_region.current
    values = { region = "eu-north-1" }
  }

  # jsondecode is the validity check: a body that is not JSON fails the run.
  assert {
    condition = length(setsubtract([
      "AWS/RDS/ServerlessDatabaseCapacity", "AWS/RDS/ACUUtilization", "AWS/RDS/CPUUtilization",
      "AWS/RDS/FreeableMemory", "AWS/RDS/DatabaseConnections",
      "ECS/ContainerInsights/CpuUtilized", "ECS/ContainerInsights/MemoryUtilized",
      "Cascade/t/Tracebacks", "AWS/Events/FailedInvocations",
    ], output.controls.dashboard_metrics)) == 0
    error_message = "Missing from the dashboard: ${join(", ", setsubtract(["AWS/RDS/ServerlessDatabaseCapacity", "AWS/RDS/ACUUtilization", "AWS/RDS/CPUUtilization", "AWS/RDS/FreeableMemory", "AWS/RDS/DatabaseConnections", "ECS/ContainerInsights/CpuUtilized", "ECS/ContainerInsights/MemoryUtilized", "Cascade/t/Tracebacks", "AWS/Events/FailedInvocations"], output.controls.dashboard_metrics))}"
  }
  # From the provider's region, never a literal and never a variable default.
  assert {
    condition = alltrue([
      for widget in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets :
      widget.properties.region == "eu-north-1" if widget.type == "metric"
    ])
    error_message = "Every metric widget must name the region the provider is configured for."
  }
  assert {
    condition = alltrue([
      for widget in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets :
      alltrue([for metric in widget.properties.metrics : metric[3] == "t-db" if metric[0] == "AWS/RDS"]) &&
      alltrue([for metric in widget.properties.metrics : metric[3] == "t-bench" if metric[0] == "ECS/ContainerInsights"])
      if widget.type == "metric"
    ])
    error_message = "Aurora widgets chart the named cluster; task widgets chart the ECS cluster named in cluster_arn."
  }
  assert {
    condition = one([
      for widget in jsondecode(aws_cloudwatch_dashboard.this.dashboard_body).widgets : widget
      if widget.type == "metric" && widget.properties.title == "Aurora capacity (ACU)"
    ]).properties.annotations.horizontal[0].value == 13.5
    error_message = "The capacity graph must show the ceiling it is read against."
  }
  assert {
    condition     = output.dashboard_name == "t-operations"
    error_message = "One dashboard, named for the environment."
  }
}

run "the_topic_and_key_requirements_name_exactly_these_publishers" {
  # apply, not plan: the rule and alarm ARNs are unknown until then.
  command = apply
  module {
    source = "../../modules/observability"
  }

  assert {
    condition = length([
      for s in data.aws_iam_policy_document.alerts_topic.statement : s
      if s.effect != "Deny" && s.actions == toset(["sns:Publish"]) && s.resources == toset([var.alerts_topic_arn])
    ]) == length(data.aws_iam_policy_document.alerts_topic.statement)
    error_message = "Every statement grants sns:Publish on the alerts topic and nothing else."
  }
  assert {
    condition = one([
      for s in data.aws_iam_policy_document.alerts_topic.statement : one(s.principals).identifiers
      if s.sid == "TaskFailureRulesPublish"
    ]) == toset(["events.amazonaws.com"])
    error_message = "EventBridge is the publisher for the task-failure rules."
  }
  assert {
    condition = one([
      for s in data.aws_iam_policy_document.alerts_topic.statement : [
        for c in s.condition : c.values if c.variable == "aws:SourceArn" && c.test == "ArnEquals"
      ] if s.sid == "TaskFailureRulesPublish"
    ]) == [tolist([aws_cloudwatch_event_rule.task_exited.arn, aws_cloudwatch_event_rule.task_never_started.arn])]
    error_message = "EventBridge may publish for these two rules only (aws:SourceArn)."
  }
  assert {
    condition     = aws_cloudwatch_event_rule.task_exited.arn != aws_cloudwatch_event_rule.task_never_started.arn && output.event_rule_arn == aws_cloudwatch_event_rule.task_exited.arn
    error_message = "Guard the guard: the two rule ARNs must differ for the assertion above to mean both."
  }
  assert {
    condition = toset(flatten([
      for s in data.aws_iam_policy_document.alerts_topic.statement : [
        for c in s.condition : c.values if c.variable == "aws:SourceArn"
      ] if s.sid == "ObservabilityAlarmsPublish"
    ])) == toset([for alarm in aws_cloudwatch_metric_alarm.this : alarm.arn])
    error_message = "CloudWatch may publish for exactly this module's alarms."
  }
  # AWS: source conditions on the KMS policy are "not supported for
  # EventBridge-to-encrypted topics". One here fails delivery with no error.
  assert {
    condition = one([
      for s in data.aws_iam_policy_document.alerts_key.statement : length(s.condition)
      if contains(one(s.principals).identifiers, "events.amazonaws.com")
    ]) == 0
    error_message = "The EventBridge key statement must carry no source condition."
  }
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.alerts_key.statement :
      s.actions == toset(["kms:Decrypt", "kms:GenerateDataKey*"])
    ])
    error_message = "Publishing to an encrypted topic needs GenerateDataKey* and Decrypt, and nothing more."
  }
}

run "an_invented_memory_floor_is_not_accepted" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  variables {
    freeable_memory_low_bytes = 0
  }
  expect_failures = [var.freeable_memory_low_bytes]
}

run "a_memory_floor_above_all_the_memory_there_is_is_refused" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  # 64 GiB against 13.5 ACU (~27 GiB): it would sit in ALARM for ever.
  variables {
    freeable_memory_low_bytes = 68719476736
  }
  expect_failures = [var.freeable_memory_low_bytes]
}

run "an_invented_connection_ceiling_is_not_accepted" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  variables {
    database_connections_high = 0
  }
  expect_failures = [var.database_connections_high]
}

run "a_cluster_name_is_not_a_cluster_arn" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  # modules/bench outputs the name. In an event pattern it would match nothing.
  variables {
    cluster_arn = "t-bench"
  }
  expect_failures = [var.cluster_arn]
}

run "minimum_capacity_above_maximum_is_refused" {
  command = plan
  module {
    source = "../../modules/observability"
  }
  variables {
    min_acu = 16
  }
  expect_failures = [var.min_acu]
}
