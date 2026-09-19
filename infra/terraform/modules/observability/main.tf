# Observability: what closes "task logs reach CloudWatch, but nothing watches
# them" (docs/architecture/well-architected.md, Operational excellence).
#
# Four things are watched, and each answers a different question:
#   * a task stopped badly      -> WHICH exit code, because here they mean
#                                  different things (CLAUDE.md section 4);
#   * a traceback was logged    -> the one failure class that is always a bug;
#   * Aurora ran out of room    -> the measurement is capacity-bound, so the
#                                  number it produced measures the ceiling;
#   * a state machine failed    -> a fan-out stopped and nobody was at a terminal.
# And one thing about the watching itself: whether an alert, once matched, was
# actually delivered.
#
# Everything alerts through the one topic modules/governance owns. This module
# creates no topic and no topic policy: two policy resources on one topic
# overwrite each other. What the topic and its key must allow is an output.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account      = data.aws_caller_identity.current.account_id
  region       = data.aws_region.current.region
  cluster_name = one(regex("cluster/(.+)$", var.cluster_arn))

  # CLAUDE.md section 4, carried into the alert so whoever reads it at 2 a.m.
  # does not need the repository open. The constants live in cascade/version.py.
  exit_code_meanings = {
    "1" = "unexpected error - a bug, unless the command line itself was refused; the traceback is in the task log"
    "2" = "budget ceiling breached - the cost meter stopped this phase on purpose; it checkpointed and resumes"
    "3" = "precondition failed - an input is missing or a gate refused; nothing is broken"
    "4" = "cache miss in replay - a recording the run needs does not exist"
  }

  # EventBridge renders a text target line by line, each line its own quoted
  # string. No line may contain a double quote or an angle bracket of its own:
  # quotes end the string, and <...> is a placeholder.
  task_exited_lines = concat(
    [
      "Cascade task stopped with a non-zero exit code.",
      "Exit code: <exit_code> (container <container>)",
      "What the exit codes mean:",
    ],
    [for code in sort(keys(local.exit_code_meanings)) : "  ${code} = ${local.exit_code_meanings[code]}"],
    [
      "  above 128 = killed by a signal (the code minus 128): 137 is SIGKILL, on Fargate usually out of memory; 143 is SIGTERM, a stop from outside",
      "Stopped reason: <stopped_reason>",
      "Container reason: <container_reason>",
      "Stop code: <stop_code>",
      "Task: <task_arn>",
      "Task definition: <task_definition_arn>",
      "Started by: <started_by>",
      "Stopped at: <stopped_at>",
    ],
  )

  task_never_started_lines = [
    "Cascade task never started. There is no exit code because no container ran.",
    "Stopped reason: <stopped_reason>",
    "Stop code: <stop_code>",
    "Task: <task_arn>",
    "Task definition: <task_definition_arn>",
    "Started by: <started_by>",
    "Stopped at: <stopped_at>",
  ]

  task_paths = {
    stopped_reason      = "$.detail.stoppedReason"
    stop_code           = "$.detail.stopCode"
    task_arn            = "$.detail.taskArn"
    task_definition_arn = "$.detail.taskDefinitionArn"
    started_by          = "$.detail.startedBy"
    stopped_at          = "$.detail.stoppedAt"
  }
}

# --- Task failure -> alert, exit code intact ----------------------------------------

# `anything-but: [0]` only matches a container that HAS an exit code, so a clean
# exit never alerts and neither does a task still running. It matches if any
# container in the task exited non-zero.
resource "aws_cloudwatch_event_rule" "task_exited" {
  name        = "${var.name}-task-exited-nonzero"
  description = "A task in ${local.cluster_name} stopped with a non-zero container exit code"

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [var.cluster_arn]
      lastStatus = ["STOPPED"]
      containers = { exitCode = [{ "anything-but" = [0] }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "task_exited" {
  rule      = aws_cloudwatch_event_rule.task_exited.name
  target_id = "alerts"
  arn       = var.alerts_topic_arn

  input_transformer {
    # containers[0]: the task definition has one container (modules/bench). A
    # second container would need its own path; the pattern above already
    # matches on any of them.
    # A path the event lacks renders as nothing, so the optional ones are safe:
    # `reason` is present for an out-of-memory kill and absent for a plain exit.
    input_paths = merge(local.task_paths, {
      exit_code        = "$.detail.containers[0].exitCode"
      container        = "$.detail.containers[0].name"
      container_reason = "$.detail.containers[0].reason"
    })
    # Not jsonencode: it escapes < and >, which would turn every placeholder
    # into literal text.
    input_template = join("\n", [for line in local.task_exited_lines : "\"${line}\""])
  }
}

# The hole the rule above leaves: a task that cannot pull its image or resolve
# its secrets stops with no container exit code at all, so `anything-but`
# has nothing to match and the failure would be silent. It is also the likeliest
# first failure in a VPC with no internet path.
resource "aws_cloudwatch_event_rule" "task_never_started" {
  name        = "${var.name}-task-never-started"
  description = "A task in ${local.cluster_name} stopped before any container ran"

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [var.cluster_arn]
      lastStatus = ["STOPPED"]
      stopCode   = ["TaskFailedToStart"]
    }
  })
}

resource "aws_cloudwatch_event_target" "task_never_started" {
  rule      = aws_cloudwatch_event_rule.task_never_started.name
  target_id = "alerts"
  arn       = var.alerts_topic_arn

  input_transformer {
    input_paths    = local.task_paths
    input_template = join("\n", [for line in local.task_never_started_lines : "\"${line}\""])
  }
}

# --- Tracebacks ---------------------------------------------------------------------

# Exit 2 and exit 3 are the system working. A traceback never is: cli.py's
# main() catches every deliberate stop (budget, cache miss, precondition) and
# prints one line for it, so an exception nothing anticipated is the only thing
# that writes this word to the log.
resource "aws_cloudwatch_log_metric_filter" "traceback" {
  name           = "${var.name}-traceback"
  log_group_name = var.task_log_group_name
  pattern        = "Traceback"

  metric_transformation {
    name      = "Tracebacks"
    namespace = "Cascade/${var.name}"
    value     = "1"
    unit      = "Count"
  }
}

# --- Alarms -------------------------------------------------------------------------

locals {
  rds = { DBClusterIdentifier = var.db_cluster_identifier }

  # The bench pins capacity (min = max) so that a p95 never measures the
  # scaler. On a pinned cluster ServerlessDatabaseCapacity equals max_acu and
  # ACUUtilization equals 100 from creation to destruction: alarms on them
  # would fire the moment the sandbox exists and teach everyone to ignore the
  # topic. They are created only where capacity can actually move.
  capacity_scales = var.min_acu < var.max_acu

  scaling_alarms = { for key, alarm in {
    aurora-capacity-at-max = {
      namespace           = "AWS/RDS"
      metric_name         = "ServerlessDatabaseCapacity"
      dimensions          = local.rds
      statistic           = "Minimum"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      threshold           = var.max_acu
      evaluation_periods  = var.sustained_minutes
      description         = "Aurora capacity has not dropped below max_acu (${var.max_acu}) for ${var.sustained_minutes} minutes: the workload wants more than the ceiling allows. Anything measured in this window is capacity-bound -- it measures the ceiling, not the query."
    }
    aurora-acu-utilization-high = {
      namespace           = "AWS/RDS"
      metric_name         = "ACUUtilization"
      dimensions          = local.rds
      statistic           = "Average"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      threshold           = var.acu_utilization_high_percent
      evaluation_periods  = var.sustained_minutes
      description         = "Aurora is within reach of max_acu (${var.max_acu}): the early warning for aurora-capacity-at-max."
    }
  } : key => alarm if local.capacity_scales }

  aurora_alarms = {
    # The capacity-bound signal that still means something when capacity is
    # pinned: CPU is measured against what max_acu provides.
    aurora-cpu-high = {
      namespace           = "AWS/RDS"
      metric_name         = "CPUUtilization"
      dimensions          = local.rds
      statistic           = "Average"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      threshold           = var.cpu_utilization_high_percent
      evaluation_periods  = var.sustained_minutes
      description         = "Aurora CPU is near everything max_acu (${var.max_acu}) provides. With capacity pinned for a measurement this is what capacity-bound looks like: the number measures the instance size, not the query."
    }
    aurora-freeable-memory-low = {
      namespace           = "AWS/RDS"
      metric_name         = "FreeableMemory"
      dimensions          = local.rds
      statistic           = "Average"
      comparison_operator = "LessThanThreshold"
      threshold           = var.freeable_memory_low_bytes
      evaluation_periods  = var.sustained_minutes
      description         = "Aurora is short of memory at max_acu (${var.max_acu}). M8 measured memory, not the index, as what bound retrieval p95 locally: once the HNSW indexes stop fitting, every query pays for disk."
    }
    # One datapoint, not a sustained window: past max_connections the next
    # connect is refused, and a refused connect is a failed phase.
    aurora-connections-high = {
      namespace           = "AWS/RDS"
      metric_name         = "DatabaseConnections"
      dimensions          = local.rds
      statistic           = "Maximum"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      threshold           = var.database_connections_high
      evaluation_periods  = 1
      description         = "Aurora connections are near the limit a phase can open before connects are refused."
    }
  }

  task_alarms = {
    # The namespace and name are read back from the filter, so the alarm cannot
    # end up watching a metric nothing emits.
    task-traceback = {
      namespace           = one(aws_cloudwatch_log_metric_filter.traceback.metric_transformation).namespace
      metric_name         = one(aws_cloudwatch_log_metric_filter.traceback.metric_transformation).name
      dimensions          = {}
      statistic           = "Sum"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      threshold           = 1
      evaluation_periods  = 1
      description         = "A task logged a Python traceback. Deliberate exits (2 budget, 3 precondition, 4 cache miss) print one line and never a traceback, so this is always a bug. Log group: ${var.task_log_group_name}"
    }
  }

  # Who watches the watcher. If the topic policy or the key policy this module
  # outputs was never added, EventBridge drops the alert and says so only here;
  # likewise if a stopped reason ever carries a double quote, which EventBridge
  # does not escape and the text template cannot survive. The notification may
  # share that fate; the alarm state on the dashboard does not.
  delivery_alarms = { for key, rule in {
    alert-delivery-task-exited        = aws_cloudwatch_event_rule.task_exited
    alert-delivery-task-never-started = aws_cloudwatch_event_rule.task_never_started
    } : key => {
    namespace           = "AWS/Events"
    metric_name         = "FailedInvocations"
    dimensions          = { RuleName = rule.name }
    statistic           = "Sum"
    comparison_operator = "GreaterThanOrEqualToThreshold"
    threshold           = 1
    evaluation_periods  = 1
    description         = "EventBridge matched a stopped task and could not deliver the alert (rule ${rule.name}). Check that the alerts topic policy and its KMS key policy carry this module's statements."
  } }

  sfn_metrics = {
    ExecutionsAborted  = "was stopped from outside before it finished"
    ExecutionsFailed   = "failed in a state nothing caught"
    ExecutionsTimedOut = "ran past its timeout"
  }

  sfn_alarms = merge([for index, arn in var.state_machine_arns : {
    for metric in sort(keys(local.sfn_metrics)) : "sfn-${index}-${metric}" => {
      namespace           = "AWS/States"
      metric_name         = metric
      dimensions          = { StateMachineArn = arn }
      statistic           = "Sum"
      comparison_operator = "GreaterThanOrEqualToThreshold"
      threshold           = 1
      evaluation_periods  = 1
      description         = "An execution of ${arn} ${local.sfn_metrics[metric]}. Phases resume from their last completed unit (invariant 8): start a new execution once the cause is known."
    }
  }]...)

  # Every alarm in this module is in this map, so the one resource below is
  # the only place an alarm is wired to the topic -- or could fail to be.
  alarms = merge(local.scaling_alarms, local.aurora_alarms, local.task_alarms, local.delivery_alarms, local.sfn_alarms)
}

resource "aws_cloudwatch_metric_alarm" "this" {
  for_each = local.alarms

  alarm_name          = "${var.name}-${each.key}"
  alarm_description   = each.value.description
  namespace           = each.value.namespace
  metric_name         = each.value.metric_name
  dimensions          = each.value.dimensions
  statistic           = each.value.statistic
  period              = 60
  evaluation_periods  = each.value.evaluation_periods
  comparison_operator = each.value.comparison_operator
  threshold           = each.value.threshold

  alarm_actions = [var.alerts_topic_arn]
  ok_actions    = [var.alerts_topic_arn]

  # Right for every alarm above, for two different reasons. The Aurora metrics
  # exist only while a cluster does, and this environment is created, measured
  # and destroyed: absence is the normal state, and INSUFFICIENT_DATA between
  # measurements is noise. The count metrics (tracebacks, failed executions,
  # failed deliveries) are emitted only when the bad thing happens, so for them
  # absence is not merely normal, it is the good case.
  #
  # It would be WRONG for a liveness alarm -- "the ingest should be writing and
  # is not" -- where missing data is the signal itself. There is none here; one
  # added later must not join this map.
  treat_missing_data = "notBreaching"
}

# --- What the topic and its key must allow ----------------------------------------------

# Without these, every resource above applies cleanly and no alert is ever
# delivered. modules/governance replaces the topic's default policy with one
# that names the two cost services only, so publishers must be added to it.
data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid       = "TaskFailureRulesPublish"
    actions   = ["sns:Publish"]
    resources = [var.alerts_topic_arn]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
    # These two rules and no others: without it any rule in any account that
    # learned the topic ARN could publish here (the confused-deputy problem).
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.task_exited.arn, aws_cloudwatch_event_rule.task_never_started.arn]
    }
  }

  statement {
    sid       = "ObservabilityAlarmsPublish"
    actions   = ["sns:Publish"]
    resources = [var.alerts_topic_arn]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [for key in sort(keys(aws_cloudwatch_metric_alarm.this)) : aws_cloudwatch_metric_alarm.this[key].arn]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }
}

# The topic is encrypted with a CMK, and a publisher that cannot use the key
# fails exactly as quietly as one the topic policy refuses.
data "aws_iam_policy_document" "alerts_key" {
  #checkov:skip=CKV_AWS_356:Resource "*" in a KMS key policy means the key the policy is attached to, not all resources; this document is output for the alerts key's policy and attached nowhere else.
  #checkov:skip=CKV_AWS_111:As above: kms:GenerateDataKey* is scoped to the one key whose policy carries this statement, and to two AWS service principals.
  statement {
    sid       = "AlarmsUseTheAlertsKey"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
  }

  # No condition, deliberately. The SNS guide: "Adding the aws:SourceAccount,
  # aws:SourceArn, and aws:SourceOrgID to a AWS KMS policy is not supported for
  # EventBridge-to-encrypted topics." The platform key's existing statement
  # carries aws:SourceAccount; copying that pattern here would break delivery
  # with no error. The topic policy above is what confines EventBridge.
  statement {
    sid       = "EventBridgeUsesTheAlertsKey"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

# --- Dashboard ----------------------------------------------------------------------

locals {
  has_machines = length(var.state_machine_arns) > 0

  widget_defaults = { region = local.region, view = "timeSeries", period = 60 }

  aurora_widgets = [
    {
      type = "metric", x = 0, y = 0, width = 6, height = 6
      properties = merge(local.widget_defaults, {
        title   = "Aurora capacity (ACU)"
        stat    = "Average"
        metrics = [["AWS/RDS", "ServerlessDatabaseCapacity", "DBClusterIdentifier", var.db_cluster_identifier]]
        # The ceiling on the graph it matters to: a flat line on it is the
        # capacity-bound measurement, visible without reading a number.
        annotations = { horizontal = [{ label = "max_acu", value = var.max_acu }] }
      })
    },
    {
      type = "metric", x = 6, y = 0, width = 6, height = 6
      properties = merge(local.widget_defaults, {
        title = "Aurora utilisation (%)"
        stat  = "Average"
        metrics = [
          ["AWS/RDS", "ACUUtilization", "DBClusterIdentifier", var.db_cluster_identifier],
          ["AWS/RDS", "CPUUtilization", "DBClusterIdentifier", var.db_cluster_identifier],
        ]
        yAxis = { left = { min = 0, max = 100 } }
      })
    },
    {
      type = "metric", x = 12, y = 0, width = 6, height = 6
      properties = merge(local.widget_defaults, {
        title       = "Aurora freeable memory (bytes)"
        stat        = "Average"
        metrics     = [["AWS/RDS", "FreeableMemory", "DBClusterIdentifier", var.db_cluster_identifier]]
        annotations = { horizontal = [{ label = "alarm floor", value = var.freeable_memory_low_bytes }] }
      })
    },
    {
      type = "metric", x = 18, y = 0, width = 6, height = 6
      properties = merge(local.widget_defaults, {
        title       = "Aurora connections"
        stat        = "Maximum"
        metrics     = [["AWS/RDS", "DatabaseConnections", "DBClusterIdentifier", var.db_cluster_identifier]]
        annotations = { horizontal = [{ label = "alarm ceiling", value = var.database_connections_high }] }
      })
    },
  ]

  # Container Insights (enabled on the cluster in modules/bench). Utilised
  # against reserved: a bench task at its memory reservation is the exit 137
  # the task alert explains.
  task_widgets = [
    {
      type = "metric", x = 0, y = 6, width = 12, height = 6
      properties = merge(local.widget_defaults, {
        title = "Task CPU (units)"
        stat  = "Average"
        metrics = [
          ["ECS/ContainerInsights", "CpuUtilized", "ClusterName", local.cluster_name],
          ["ECS/ContainerInsights", "CpuReserved", "ClusterName", local.cluster_name],
        ]
      })
    },
    {
      type = "metric", x = 12, y = 6, width = 12, height = 6
      properties = merge(local.widget_defaults, {
        title = "Task memory (MiB)"
        stat  = "Average"
        metrics = [
          ["ECS/ContainerInsights", "MemoryUtilized", "ClusterName", local.cluster_name],
          ["ECS/ContainerInsights", "MemoryReserved", "ClusterName", local.cluster_name],
        ]
      })
    },
  ]

  failure_widgets = [
    {
      type = "metric", x = 0, y = 12, width = 12, height = 6
      properties = merge(local.widget_defaults, {
        title = "Tracebacks logged (always a bug)"
        stat  = "Sum"
        metrics = [[
          one(aws_cloudwatch_log_metric_filter.traceback.metric_transformation).namespace,
          one(aws_cloudwatch_log_metric_filter.traceback.metric_transformation).name,
        ]]
      })
    },
    {
      type = "metric", x = 12, y = 12, width = 12, height = 6
      properties = merge(local.widget_defaults, {
        title = "Stopped-task alerts: matched and undelivered"
        stat  = "Sum"
        metrics = [
          ["AWS/Events", "TriggeredRules", "RuleName", aws_cloudwatch_event_rule.task_exited.name],
          ["AWS/Events", "TriggeredRules", "RuleName", aws_cloudwatch_event_rule.task_never_started.name],
          ["AWS/Events", "FailedInvocations", "RuleName", aws_cloudwatch_event_rule.task_exited.name],
          ["AWS/Events", "FailedInvocations", "RuleName", aws_cloudwatch_event_rule.task_never_started.name],
        ]
      })
    },
  ]

  sfn_metric_widget = {
    type = "metric", x = 0, y = 18, width = 24, height = 6
    properties = merge(local.widget_defaults, {
      title = "Step Functions executions"
      stat  = "Sum"
      # concat, not flatten: flatten would also flatten each metric's own array.
      metrics = concat([], [for arn in var.state_machine_arns : [
        for metric in concat(["ExecutionsStarted", "ExecutionsSucceeded"], sort(keys(local.sfn_metrics))) :
        ["AWS/States", metric, "StateMachineArn", arn]
      ]]...)
    })
  }

  # A metric widget with no metrics is refused by PutDashboard, so until a
  # state machine exists the row says why it is empty.
  sfn_text_widget = {
    type = "text", x = 0, y = 18, width = 24, height = 2
    properties = {
      markdown = "**Step Functions executions** -- no state machine is watched yet. The ingest and simulation fan-outs are designed, not written; pass their ARNs as `state_machine_arns`."
    }
  }

  widgets = concat(
    local.aurora_widgets,
    local.task_widgets,
    local.failure_widgets,
    [for widget in [local.sfn_metric_widget] : widget if local.has_machines],
    [for widget in [local.sfn_text_widget] : widget if !local.has_machines],
  )
}

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = "${var.name}-operations"
  dashboard_body = jsonencode({ widgets = local.widgets })
}
