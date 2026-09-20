# The pipeline, offline: two Step Functions chains over the existing CLI
# phases. What is asserted is the shape a supervisor depends on -- the order,
# that every failure is announced and ends FAILED, that the budget estimate
# cannot be stepped around, and that nothing runs in parallel that the code
# cannot run in parallel.
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
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:mock" }
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
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:mock" }
  }
}

variables {
  name                    = "t"
  cluster_arn             = "arn:aws:ecs:us-east-1:123456789012:cluster/mock"
  task_definition_arn     = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:7"
  task_execution_role_arn = "arn:aws:iam::123456789012:role/mock-execution"
  task_role_arn           = "arn:aws:iam::123456789012:role/mock-task"
  subnet_ids              = ["subnet-0aaa", "subnet-0bbb"]
  security_group_ids      = ["sg-0ccc"]
  alerts_topic_arn        = "arn:aws:sns:us-east-1:123456789012:mock"
  kms_key_arn             = "arn:aws:kms:us-east-1:123456789012:key/mock"
  fanout_timeout_seconds  = 1209600
}

run "the_ingest_is_the_hand_run_chain_in_order" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.this["ingest"].definition).StartAt == "CorpusBuild"
    error_message = "The ingest starts with the build."
  }
  assert {
    condition = [
      for state in ["CorpusBuild", "RetrievalIndex", "RetrievalVerify", "CorpusVerify", "CorpusCoverage"] :
      jsondecode(aws_sfn_state_machine.this["ingest"].definition).States[state].Next
    ] == ["RetrievalIndex", "RetrievalVerify", "CorpusVerify", "CorpusCoverage", "Done"]
    error_message = "build -> index -> retrieval verify -> corpus verify -> coverage -> Done, in that order: the verdicts are about the indexed corpus."
  }
  assert {
    condition = [
      for state in ["CorpusBuild", "RetrievalIndex", "RetrievalVerify", "CorpusVerify", "CorpusCoverage"] :
      join(" ", one(jsondecode(aws_sfn_state_machine.this["ingest"].definition).States[state].Parameters.Overrides.ContainerOverrides).Command)
    ] == ["cascade corpus build", "cascade retrieval index --drop-legacy", "cascade retrieval verify", "cascade corpus verify", "cascade corpus coverage"]
    error_message = "Each state must run the documented CLI phase."
  }
  assert {
    condition = length([
      for name, state in jsondecode(aws_sfn_state_machine.this["ingest"].definition).States : name
      if endswith(try(state.Resource, ""), ":ecs:runTask.sync")
    ]) == 5
    error_message = "Five phases, no more: a sixth task would be a phase nobody reviewed."
  }
}

run "the_study_is_gated_by_its_budget_estimate" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }

  assert {
    condition     = jsondecode(aws_sfn_state_machine.this["study"].definition).StartAt == "SimulateEstimate"
    error_message = "Spec 12.4: no full phase launches without the estimate."
  }
  assert {
    condition = [
      for state in ["SimulateEstimate", "SimulateAll", "EnsembleCollapse", "EvalGrid", "Report"] :
      jsondecode(aws_sfn_state_machine.this["study"].definition).States[state].Next
    ] == ["SimulateAll", "EnsembleCollapse", "EvalGrid", "Report", "Done"]
    error_message = "estimate -> simulate -> collapse -> grid -> report -> Done, in that order."
  }
  assert {
    condition = [
      for state in ["SimulateEstimate", "SimulateAll", "EnsembleCollapse", "EvalGrid", "Report"] :
      join(" ", one(jsondecode(aws_sfn_state_machine.this["study"].definition).States[state].Parameters.Overrides.ContainerOverrides).Command)
    ] == ["cascade simulate estimate --units 20", "cascade simulate all --wave 200", "cascade ensemble collapse", "cascade eval grid", "cascade report"]
    error_message = "Each state must run the documented CLI phase."
  }
  assert {
    condition = length([
      for name, state in jsondecode(aws_sfn_state_machine.this["study"].definition).States : name
      if endswith(try(state.Resource, ""), ":ecs:runTask.sync")
    ]) == 5
    error_message = "Five phases, no more."
  }
  # Every edge into the fan-out, of either kind. A Catch on the estimate that
  # led here would turn "projected breach, exit 2" into "proceed".
  assert {
    condition = flatten([
      for name, state in jsondecode(aws_sfn_state_machine.this["study"].definition).States : [
        for target in concat([try(state.Next, "")], [for catch in try(state.Catch, []) : catch.Next], [try(state.Default, "")]) :
        "${name}:${target == try(state.Next, "") ? "next" : "other"}" if target == "SimulateAll"
      ]
    ]) == ["SimulateEstimate:next"]
    error_message = "The only way into SimulateAll is the estimate succeeding."
  }
  assert {
    condition = alltrue([
      for catch in jsondecode(aws_sfn_state_machine.this["study"].definition).States.SimulateEstimate.Catch :
      catch.Next == "SimulateEstimateFailed"
    ])
    error_message = "A failed estimate goes to the failure path and nowhere else."
  }
}

run "every_task_announces_its_own_failure" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }

  # Guard the guard: the loops below must be looping over something.
  assert {
    condition = length(flatten([
      for machine in ["ingest", "study"] : [
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States : name
        if endswith(try(state.Resource, ""), ":ecs:runTask.sync")
      ]
    ])) == 10
    error_message = "Expected ten ECS task states across the two machines."
  }
  # Every one of them, not a sample: exactly one catcher, it matches
  # everything, and it leads to a Pass that records THIS state's name before
  # the alert.
  assert {
    condition = alltrue(flatten([
      for machine in ["ingest", "study"] : [
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States :
        try(
          length(state.Catch) == 1
          && state.Catch[0].ErrorEquals == ["States.ALL"]
          && jsondecode(aws_sfn_state_machine.this[machine].definition).States[state.Catch[0].Next].Type == "Pass"
          && jsondecode(aws_sfn_state_machine.this[machine].definition).States[state.Catch[0].Next].Parameters.state == name
          && jsondecode(aws_sfn_state_machine.this[machine].definition).States[state.Catch[0].Next].Parameters["failure.$"] == state.Catch[0].ResultPath
          && jsondecode(aws_sfn_state_machine.this[machine].definition).States[state.Catch[0].Next].Next == "NotifyFailure",
          false
        )
        if endswith(try(state.Resource, ""), ":ecs:runTask.sync")
      ]
    ]))
    error_message = "Every ECS task state must Catch States.ALL into a Pass that names it and leads to NotifyFailure."
  }
  # And no Task of any kind -- the alert included -- is left without one.
  assert {
    condition = alltrue(flatten([
      for machine in ["ingest", "study"] : [
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States :
        contains(flatten([for catch in try(state.Catch, []) : catch.ErrorEquals]), "States.ALL")
        if state.Type == "Task"
      ]
    ]))
    error_message = "A Task without a Catch fails the execution without telling anyone."
  }
  assert {
    condition = alltrue([
      for machine in ["ingest", "study"] :
      endswith(jsondecode(aws_sfn_state_machine.this[machine].definition).States.NotifyFailure.Resource, ":sns:publish")
      && jsondecode(aws_sfn_state_machine.this[machine].definition).States.NotifyFailure.Parameters.TopicArn == var.alerts_topic_arn
      && strcontains(jsondecode(aws_sfn_state_machine.this[machine].definition).States.NotifyFailure.Parameters["Message.$"], "$.failed")
      && strcontains(jsondecode(aws_sfn_state_machine.this[machine].definition).States.NotifyFailure.Parameters["Subject.$"], "$.failed.state")
    ])
    error_message = "The alert goes to the alerts topic and carries the recorded failure."
  }
  assert {
    condition = alltrue(flatten([
      for machine in ["ingest", "study"] : [
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States :
        state.Parameters["execution.$"] == "$$.Execution.Name" && state.Parameters["failure.$"] == "$.failure"
        if state.Type == "Pass"
      ]
    ]))
    error_message = "The recorded failure carries the execution name and the error with its cause."
  }
}

run "a_failed_chain_never_reads_as_finished" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }

  assert {
    condition = alltrue([
      for machine in ["ingest", "study"] :
      jsondecode(aws_sfn_state_machine.this[machine].definition).States.NotifyFailure.Next == "Failed"
      && length(try(jsondecode(aws_sfn_state_machine.this[machine].definition).States.NotifyFailure.Catch, [])) > 0
      && alltrue([for catch in try(jsondecode(aws_sfn_state_machine.this[machine].definition).States.NotifyFailure.Catch, []) : catch.Next == "Failed"])
      && jsondecode(aws_sfn_state_machine.this[machine].definition).States.Failed.Type == "Fail"
    ])
    error_message = "Both edges out of the alert must reach a Fail state: an alert that cannot be sent is still a failure."
  }
  # The only terminals are one Succeed and one Fail; nothing ends in place.
  assert {
    condition = alltrue([
      for machine in ["ingest", "study"] :
      length([for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States : name if can(state.End)]) == 0
      && [for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States : name if state.Type == "Succeed"] == ["Done"]
      && [for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States : name if state.Type == "Fail"] == ["Failed"]
    ])
    error_message = "Exactly one Succeed, exactly one Fail, and no state that ends the execution itself."
  }
  # Every edge into Succeed, of either kind: only the last phase, only on success.
  assert {
    condition = {
      for machine in ["ingest", "study"] : machine => flatten([
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States : [
          for target in concat([try(state.Next, "")], [for catch in try(state.Catch, []) : catch.Next]) :
          name if target == "Done"
        ]
      ])
    } == { ingest = ["CorpusCoverage"], study = ["Report"] }
    error_message = "Only the last phase's success may reach Succeed."
  }
}

run "only_the_build_is_re_run_and_never_forever" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }

  # States.TaskFailed is every non-zero exit, including exit 2. The cost meter
  # starts each process at zero, so re-running a spending phase re-grants its
  # ceiling: no study state may do it.
  assert {
    condition = {
      for machine in ["ingest", "study"] : machine => [
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States : name
        if endswith(try(state.Resource, ""), ":ecs:runTask.sync") && length(setintersection(
          flatten([for retry in try(state.Retry, []) : retry.ErrorEquals]),
          ["States.ALL", "States.TaskFailed", "States.Timeout"]
        )) > 0
      ]
    } == { ingest = ["CorpusBuild"], study = [] }
    error_message = "Only `corpus build` re-runs a task that ran and failed; a verdict, a breach or a timeout is never retried."
  }
  assert {
    condition = alltrue(flatten([
      for machine in ["ingest", "study"] : [
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States : [
          for retry in try(state.Retry, []) :
          try(retry.MaxAttempts >= 1 && retry.MaxAttempts <= 5 && retry.BackoffRate > 1 && retry.IntervalSeconds >= 5, false)
        ]
      ]
    ]))
    error_message = "Every Retry declares a small MaxAttempts and backs off."
  }
  # Bounded retries are not the only way to loop: nothing may point back at the start.
  assert {
    condition = alltrue([
      for machine in ["ingest", "study"] : length(flatten([
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States : [
          for target in concat([try(state.Next, "")], [for catch in try(state.Catch, []) : catch.Next], [try(state.Default, "")]) :
          name if target == jsondecode(aws_sfn_state_machine.this[machine].definition).StartAt
        ]
      ])) == 0
    ])
    error_message = "No edge returns to the first state: the build stops itself and the machine must not restart it."
  }
}

run "nothing_fans_out" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }

  # The ingest has no unit claiming and a per-process dedupe index; the
  # fan-out keeps its wavefront in memory. Neither survives being split.
  assert {
    condition = alltrue([
      for machine in ["ingest", "study"] :
      length(setsubtract(
        [for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States : state.Type],
        ["Task", "Pass", "Succeed", "Fail"]
      )) == 0
    ])
    error_message = "No Map, Parallel, Choice or Wait: each machine is a serial chain."
  }
  assert {
    condition = length([
      for name, state in jsondecode(aws_sfn_state_machine.this["study"].definition).States : name
      if strcontains(join(" ", try(one(state.Parameters.Overrides.ContainerOverrides).Command, [])), "simulate all")
    ]) == 1
    error_message = "The 24 steps are one process holding the wavefront in memory (ADR-0020): one state."
  }
}

run "tasks_are_private_pinned_and_bounded" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }

  assert {
    condition = alltrue(flatten([
      for machine in ["ingest", "study"] : [
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States :
        try(
          state.Parameters.NetworkConfiguration.AwsvpcConfiguration.AssignPublicIp == "DISABLED"
          && tolist(state.Parameters.NetworkConfiguration.AwsvpcConfiguration.Subnets) == var.subnet_ids
          && tolist(state.Parameters.NetworkConfiguration.AwsvpcConfiguration.SecurityGroups) == var.security_group_ids
          && state.Parameters.LaunchType == "FARGATE"
          && state.Parameters.Cluster == var.cluster_arn
          && state.Parameters.TaskDefinition == var.task_definition_arn
          && one(state.Parameters.Overrides.ContainerOverrides).Name == "cascade",
          false
        )
        if endswith(try(state.Resource, ""), ":ecs:runTask.sync")
      ]
    ]))
    error_message = "Every task: no public address, the given subnets and groups, Fargate, the pinned revision, the cascade container."
  }
  assert {
    condition = alltrue(flatten([
      for machine in ["ingest", "study"] : [
        for name, state in jsondecode(aws_sfn_state_machine.this[machine].definition).States :
        try(state.TimeoutSeconds > 0, false) && !can(state.HeartbeatSeconds)
        if endswith(try(state.Resource, ""), ":ecs:runTask.sync")
      ]
    ]))
    error_message = "Every task has a stop rule, and none expects a heartbeat the CLI cannot send."
  }
}

run "the_dials_reach_the_commands" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }
  variables {
    simulate_wave          = 36000
    estimate_units         = 5
    fanout_timeout_seconds = 777
    step_timeout_seconds   = 55
    build_timeout_seconds  = 66
    study_environment      = { CASCADE_LLM__PROVIDER = "aws", CASCADE_LLM__MODE = "record" }
  }

  # Different values from the defaults, so a restated constant cannot pass.
  assert {
    condition = [
      for state in ["SimulateEstimate", "SimulateAll"] :
      join(" ", one(jsondecode(aws_sfn_state_machine.this["study"].definition).States[state].Parameters.Overrides.ContainerOverrides).Command)
    ] == ["cascade simulate estimate --units 5", "cascade simulate all --wave 36000"]
    error_message = "The wave and the sample size are the operator's dials (ADR-0020)."
  }
  assert {
    condition = [
      for state in ["SimulateEstimate", "SimulateAll", "EnsembleCollapse", "EvalGrid", "Report"] :
      jsondecode(aws_sfn_state_machine.this["study"].definition).States[state].TimeoutSeconds
    ] == [55, 777, 55, 777, 55]
    error_message = "Both wavefront phases take the fan-out timeout; the rest take the step timeout."
  }
  assert {
    condition     = jsondecode(aws_sfn_state_machine.this["ingest"].definition).States.CorpusBuild.TimeoutSeconds == 66
    error_message = "The build has its own stop rule."
  }
  assert {
    condition = alltrue([
      for state in ["SimulateEstimate", "SimulateAll", "EnsembleCollapse", "EvalGrid", "Report"] :
      one(jsondecode(aws_sfn_state_machine.this["study"].definition).States[state].Parameters.Overrides.ContainerOverrides).Environment == [
        { Name = "CASCADE_LLM__MODE", Value = "record" },
        { Name = "CASCADE_LLM__PROVIDER", Value = "aws" },
      ]
    ])
    error_message = "Every study task carries the study environment, sorted."
  }
  assert {
    condition = alltrue([
      for state in ["CorpusBuild", "RetrievalIndex", "RetrievalVerify", "CorpusVerify", "CorpusCoverage"] :
      length(one(jsondecode(aws_sfn_state_machine.this["ingest"].definition).States[state].Parameters.Overrides.ContainerOverrides).Environment) == 0
    ])
    error_message = "The study's environment must not leak into the ingest: the ingest never calls a model."
  }
}

run "the_role_can_run_one_task_and_announce_a_failure" {
  # apply, not plan: the trust policy names ARNs built from data sources.
  command = apply
  module {
    source = "../../modules/pipeline"
  }

  assert {
    condition = toset([
      for s in concat(data.aws_iam_policy_document.states.statement, data.aws_iam_policy_document.observability.statement) : s.sid
      if s.effect != "Deny" && (contains(s.resources, "*") || length(s.resources) == 0)
    ]) == toset(["DeliverLogs", "EmitTraces"])
    error_message = "Every allow names its resources, except the two API families that define no resource types."
  }
  assert {
    condition = alltrue(flatten([
      for s in data.aws_iam_policy_document.observability.statement : [
        for action in s.actions : startswith(action, s.sid == "DeliverLogs" ? "logs:" : "xray:")
      ]
      ])) && toset(flatten([for s in data.aws_iam_policy_document.observability.statement : s.actions])) == toset([
      "logs:CreateLogDelivery", "logs:DeleteLogDelivery", "logs:DescribeLogGroups", "logs:DescribeResourcePolicies",
      "logs:GetLogDelivery", "logs:ListLogDeliveries", "logs:PutResourcePolicy", "logs:UpdateLogDelivery",
      "xray:GetSamplingRules", "xray:GetSamplingTargets", "xray:PutTelemetryRecords", "xray:PutTraceSegments",
    ])
    error_message = "The unscoped statements hold log delivery and trace emission, and nothing that could ride along."
  }
  assert {
    condition = length([
      for s in concat(data.aws_iam_policy_document.states.statement, data.aws_iam_policy_document.observability.statement) : s
      if length([for action in s.actions : action if strcontains(action, "*")]) > 0
    ]) == 0
    error_message = "No wildcard actions."
  }
  assert {
    condition = [
      for s in data.aws_iam_policy_document.states.statement : join(" ", sort(s.resources))
      if contains(s.actions, "ecs:RunTask")
    ] == ["arn:aws:ecs:us-east-1:123456789012:task-definition/mock:*"]
    error_message = "RunTask is allowed on the revisions of the one family, derived from the input ARN."
  }
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.states.statement :
      length([for c in s.condition : c if c.variable == "ecs:cluster" && c.test == "ArnEquals" && join(" ", c.values) == var.cluster_arn]) == 1
      if length([for action in s.actions : action if startswith(action, "ecs:")]) > 0
    ])
    error_message = "Every ECS permission is confined to the one cluster."
  }
  assert {
    condition = [
      for s in data.aws_iam_policy_document.states.statement : join(" ", sort(s.resources))
      if contains(s.actions, "ecs:StopTask")
    ] == ["arn:aws:ecs:us-east-1:123456789012:task/mock/*"]
    error_message = "Stop and describe reach this cluster's tasks only."
  }
  assert {
    condition = [
      for s in data.aws_iam_policy_document.states.statement : {
        resources = toset(s.resources)
        service   = flatten([for c in s.condition : c.values if c.variable == "iam:PassedToService" && c.test == "StringEquals"])
      }
      if contains(s.actions, "iam:PassRole")
      ] == [{
        resources = toset([var.task_execution_role_arn, var.task_role_arn])
        service   = ["ecs-tasks.amazonaws.com"]
    }]
    error_message = "PassRole: the task's two roles, to ECS tasks, and nothing else."
  }
  assert {
    condition = [
      for s in data.aws_iam_policy_document.states.statement : join(" ", sort(s.resources))
      if length([for action in s.actions : action if startswith(action, "events:")]) > 0
    ] == ["arn:aws:events:us-east-1:123456789012:rule/StepFunctionsGetEventsForECSTaskRule"]
    error_message = "EventBridge: the one managed rule the .sync integration uses."
  }
  assert {
    condition = [
      for s in data.aws_iam_policy_document.states.statement : join(" ", sort(s.resources))
      if contains(s.actions, "sns:Publish")
    ] == [var.alerts_topic_arn]
    error_message = "Publish to the alerts topic only."
  }
  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.assume.statement :
      one(s.principals).identifiers == toset(["states.amazonaws.com"])
      && length([for c in s.condition : c if c.variable == "aws:SourceAccount"]) == 1
      && toset(flatten([for c in s.condition : c.values if c.variable == "aws:SourceArn"])) == toset([
        "arn:aws:states:us-east-1:123456789012:stateMachine:t-ingest",
        "arn:aws:states:us-east-1:123456789012:stateMachine:t-study",
      ])
    ])
    error_message = "Only Step Functions, only for these two machines in this account."
  }
  assert {
    condition     = toset([for name, machine in aws_sfn_state_machine.this : machine.role_arn]) == toset([aws_iam_role.states.arn])
    error_message = "Both machines run as the role these policies are attached to."
  }
}

run "executions_are_logged_encrypted_and_traced" {
  command = apply
  module {
    source = "../../modules/pipeline"
  }

  assert {
    condition = alltrue([
      for name, machine in aws_sfn_state_machine.this :
      contains(["ALL", "ERROR"], one(machine.logging_configuration).level)
      && one(machine.logging_configuration).include_execution_data == false
      && one(machine.logging_configuration).log_destination == "${aws_cloudwatch_log_group.states.arn}:*"
      && one(machine.tracing_configuration).enabled
    ])
    error_message = "Both machines log (without payloads) to the module's group and trace with X-Ray."
  }
  assert {
    condition     = length(aws_sfn_state_machine.this) == 2 && alltrue([for name, machine in aws_sfn_state_machine.this : machine.type == "STANDARD"])
    error_message = "Two STANDARD machines: an Express workflow ends after five minutes."
  }
  assert {
    condition     = aws_cloudwatch_log_group.states.kms_key_id == var.kms_key_arn && aws_cloudwatch_log_group.states.retention_in_days == 365
    error_message = "The log group is encrypted with the platform CMK and kept a year."
  }
  assert {
    condition     = startswith(aws_cloudwatch_log_group.states.name, "/cascade/")
    error_message = "The key policy admits CloudWatch Logs for groups under /cascade/ only."
  }
}

run "the_controls_output_reads_the_definitions" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }

  assert {
    condition     = output.controls.study.start_at == "SimulateEstimate" && output.controls.study.next["SimulateEstimate"] == "SimulateAll" && output.controls.study.commands["SimulateAll"] == "cascade simulate all --wave 200"
    error_message = "controls must let a root-level test walk the chain."
  }
  assert {
    condition = alltrue([
      for machine in ["ingest", "study"] :
      length(output.controls[machine].uncaught_tasks) == 0
      && output.controls[machine].public_ip_settings == tolist(["DISABLED"])
      && output.controls[machine].failure_terminal == "Fail"
      && toset(output.controls[machine].state_types) == toset(["Fail", "Pass", "Succeed", "Task"])
    ])
    error_message = "controls must report no uncaught task, no public address, a Fail terminal and no fan-out state."
  }
  assert {
    condition     = output.controls.ingest.reruns_failed_tasks == tolist(["CorpusBuild"]) && length(output.controls.study.reruns_failed_tasks) == 0
    error_message = "controls must report which states re-run a failed task."
  }
  assert {
    condition = alltrue([
      for machine in ["ingest", "study"] :
      output.controls[machine].logging_level == "ALL" && output.controls[machine].logs_payloads == false && output.controls[machine].tracing
    ])
    error_message = "controls must report logging and tracing as the machines carry them."
  }
}

run "an_unpinned_task_definition_is_refused" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }
  variables {
    task_definition_arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock"
  }
  expect_failures = [var.task_definition_arn]
}

run "a_name_that_would_break_the_alert_subject_is_refused" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }
  variables {
    name = "a-name-so-long-that-the-sns-subject-would-pass-one-hundred-characters"
  }
  expect_failures = [var.name]
}

run "an_invented_fanout_timeout_is_not_accepted" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }
  variables {
    fanout_timeout_seconds = 0
  }
  expect_failures = [var.fanout_timeout_seconds]
}

# --- The way out reaches the states that fetch, and no others (ADR-0042) ------------------

run "without_an_egress_tier_no_state_leaves" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }

  assert {
    condition = alltrue(flatten([
      for machine in ["ingest", "study"] : [
        for name, subnets in output.controls[machine].subnets : tolist(subnets) == tolist(var.subnet_ids)
      ]
    ]))
    error_message = "With no egress tier, every state of both chains runs in the isolated subnets."
  }
  assert {
    condition = alltrue(flatten([
      for machine in ["ingest", "study"] : [
        for name, groups in output.controls[machine].security_groups : tolist(groups) == tolist(var.security_group_ids)
      ]
    ]))
    error_message = "And carries only the task's own security groups."
  }
}

run "with_an_egress_tier_only_the_state_that_fetches_leaves" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }
  variables {
    egress_network = {
      subnet_ids         = ["subnet-egress"]
      security_group_ids = ["sg-egress"]
    }
  }

  assert {
    condition     = tolist(output.controls.ingest.subnets.CorpusBuild) == tolist(["subnet-egress"]) && tolist(output.controls.ingest.security_groups.CorpusBuild) == tolist(["sg-0ccc", "sg-egress"])
    error_message = "CorpusBuild runs in the egress subnet with the egress group added to its own."
  }
  assert {
    condition = alltrue([
      for name in ["RetrievalIndex", "RetrievalVerify", "CorpusVerify", "CorpusCoverage"] :
      tolist(output.controls.ingest.subnets[name]) == tolist(var.subnet_ids) && !contains(output.controls.ingest.security_groups[name], "sg-egress")
    ])
    error_message = "The four ingest states that never fetch stay isolated: they hold the database's admin credential."
  }
  assert {
    condition     = alltrue([for name, subnets in output.controls.study.subnets : tolist(subnets) == tolist(var.subnet_ids)])
    error_message = "Model calls do not leave unless model_calls_use_egress says the provider has no private path."
  }
}

run "model_calls_leave_only_when_told_the_provider_has_no_private_path" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }
  variables {
    egress_network = {
      subnet_ids         = ["subnet-egress"]
      security_group_ids = ["sg-egress"]
    }
    model_calls_use_egress = true
  }

  assert {
    condition = alltrue([
      for name in ["SimulateEstimate", "SimulateAll", "EvalGrid"] :
      tolist(output.controls.study.subnets[name]) == tolist(["subnet-egress"]) && contains(output.controls.study.security_groups[name], "sg-egress")
    ])
    error_message = "The three states that call a model run in the egress subnet."
  }
  assert {
    condition = alltrue([
      for name in ["EnsembleCollapse", "Report"] :
      tolist(output.controls.study.subnets[name]) == tolist(var.subnet_ids) && !contains(output.controls.study.security_groups[name], "sg-egress")
    ])
    error_message = "The collapse and the report call no model and stay inside."
  }
}

run "the_flag_alone_moves_nothing" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }
  variables {
    model_calls_use_egress = true
  }

  assert {
    condition     = alltrue([for name, subnets in output.controls.study.subnets : tolist(subnets) == tolist(var.subnet_ids)])
    error_message = "Without an egress tier there is nowhere to move a state to; the flag is inert, not an error."
  }
}

run "the_study_runs_on_its_own_task_and_the_role_may_pass_its_roles" {
  # apply, not plan: the role's resources are built from the inputs by data sources.
  command = apply
  module {
    source = "../../modules/pipeline"
  }
  variables {
    study_task = {
      task_definition_arn     = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock-study:3"
      task_execution_role_arn = "arn:aws:iam::123456789012:role/mock-execution"
      task_role_arn           = "arn:aws:iam::123456789012:role/mock-study-task"
      security_group_ids      = ["sg-study"]
    }
  }

  assert {
    condition     = alltrue([for name, arn in output.controls.study.task_definitions : arn == var.study_task.task_definition_arn])
    error_message = "Every study state runs on the study task definition."
  }
  assert {
    condition     = alltrue([for name, arn in output.controls.ingest.task_definitions : arn == var.task_definition_arn])
    error_message = "Every ingest state still runs on the bench's."
  }
  assert {
    condition     = alltrue([for name, groups in output.controls.study.security_groups : tolist(groups) == tolist(["sg-study"])])
    error_message = "With the study task's own security group."
  }
  assert {
    condition = toset(flatten([
      for s in data.aws_iam_policy_document.states.statement : s.resources if s.sid == "RunTheCascadeTaskOnly"
    ])) == toset(["arn:aws:ecs:us-east-1:123456789012:task-definition/mock:*", "arn:aws:ecs:us-east-1:123456789012:task-definition/mock-study:*"])
    error_message = "The role may run any revision of both families and no other."
  }
  assert {
    condition = toset(flatten([
      for s in data.aws_iam_policy_document.states.statement : s.resources if s.sid == "PassTheTaskRolesToEcsOnly"
    ])) == toset(["arn:aws:iam::123456789012:role/mock-execution", "arn:aws:iam::123456789012:role/mock-task", "arn:aws:iam::123456789012:role/mock-study-task"])
    error_message = "It may pass the three roles the two definitions use, and no other -- the shared execution role once."
  }
}

run "an_unpinned_study_task_definition_is_refused" {
  command = plan
  module {
    source = "../../modules/pipeline"
  }
  variables {
    study_task = {
      task_definition_arn     = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock-study"
      task_execution_role_arn = "arn:aws:iam::123456789012:role/mock-execution"
      task_role_arn           = "arn:aws:iam::123456789012:role/mock-study-task"
      security_group_ids      = ["sg-study"]
    }
  }
  expect_failures = [var.study_task]
}
