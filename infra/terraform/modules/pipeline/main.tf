# Orchestration: the two chains a person has been starting by hand, as Step
# Functions state machines. Each state is one existing CLI phase, run as one
# Fargate task on modules/bench's task definition with its command overridden.
# The machines add no logic of their own. Every verdict is still the CLI's exit
# code (0 ok, 2 budget, 3 precondition, 4 cache miss); what changes is that a
# non-zero exit stops the chain, tells someone, and leaves the execution FAILED
# -- where a shell script's `;` would have carried on.
#
# What the wiring needs from its caller before either chain can finish
# (ADR-0042). Each is an input, because each is somebody's decision:
#
# * The ingest fetches from the public internet (Common Crawl, SEC, the
#   Federal Register, Wikipedia, GDELT). The sandbox VPC has no path to it, by
#   design (ADR-0034). Given `egress_network` (modules/egress), the ONE state
#   that fetches -- CorpusBuild -- runs in the egress tier's subnet with its
#   security group added; every other ingest state stays isolated. Without it
#   `corpus build` fetches nothing, exits 0, and the chain stops where it
#   should: at `corpus verify`, exit 3.
# * modules/bench pins CASCADE_LLM__MODE=replay and its task role cannot call
#   a model. Given `study_task` (modules/study), the study chain runs on a
#   definition that can, with the LLM cache on a file system that outlives the
#   task. Without it the chain fails closed at its first state, having spent
#   nothing. `model_calls_use_egress` says whether the provider is reached
#   through an interface endpoint (false) or the egress tier (true); only the
#   three states that call a model are moved.
#
# Still true, and not fixed by wiring: the files `cascade report` writes live
# on task scratch and die with the task. The Report state proves the report
# can be written and leaves its headline in the task log -- it does not
# publish an artifact.

data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  partition = data.aws_partition.current.partition
  region    = data.aws_region.current.region
  account   = data.aws_caller_identity.current.account_id

  run_task_sync = "arn:${local.partition}:states:::ecs:runTask.sync"
  sns_publish   = "arn:${local.partition}:states:::sns:publish"

  # One entry per phase, in order. The list IS the chain: each state's Next is
  # the entry after it, so reordering the machine means reordering this list
  # and nothing else.
  #
  # `rerun_on_failure` marks the one phase whose failed task may be re-run
  # without a person looking first. See local.rerun_retry.
  #
  # `reach` says what the phase must get to beyond the VPC: "sources" (the
  # ingest's public hosts), "model" (the provider), or "none". It is declared
  # per phase, not per machine, because a path to the internet is an exposure
  # and four of the five ingest states -- which hold the database's admin
  # credential -- never fetch anything. See local.uses_egress.
  chains = {
    # The ingest is serial on purpose. There is NO Map state fanning units out,
    # because the code has nothing that would make one correct: `corpus build`
    # computes its pending units as (all units - done units) with no claim on
    # the ones it takes, so two concurrent builds would both ingest the same
    # units; and its near-duplicate index is seeded from the database once, at
    # process start, then kept in memory, so neither would see the other's
    # documents and cross-batch duplicates would survive. A serial chain that
    # is correct beats a parallel one that silently changes the dedupe
    # behaviour M2's collapse ratio was measured under. `corpus build` already
    # streams WARC files concurrently inside the one process.
    ingest = [
      { state = "CorpusBuild", command = ["cascade", "corpus", "build"], timeout = var.build_timeout_seconds, rerun_on_failure = true, reach = "sources" },
      # After the build and before anything reads: an HNSW index is built from
      # the rows present, and the verdicts below are about the indexed corpus.
      { state = "RetrievalIndex", command = ["cascade", "retrieval", "index", "--drop-legacy"], timeout = var.step_timeout_seconds, rerun_on_failure = false, reach = "none" },
      { state = "RetrievalVerify", command = ["cascade", "retrieval", "verify"], timeout = var.step_timeout_seconds, rerun_on_failure = false, reach = "none" },
      # Two gates, because neither can see what the other sees (M7): `verify`
      # cannot detect a corpus covering the wrong years, `coverage` cannot
      # detect an empty one.
      { state = "CorpusVerify", command = ["cascade", "corpus", "verify"], timeout = var.step_timeout_seconds, rerun_on_failure = false, reach = "none" },
      { state = "CorpusCoverage", command = ["cascade", "corpus", "coverage"], timeout = var.step_timeout_seconds, rerun_on_failure = false, reach = "none" },
    ]

    study = [
      # Spec 12.4: no full phase launches without the estimate. It exits 2 on a
      # projected breach; that lands in this state's Catch like any failure,
      # and the only edge into SimulateAll is this state's success.
      { state = "SimulateEstimate", command = ["cascade", "simulate", "estimate", "--units", tostring(var.estimate_units)], timeout = var.step_timeout_seconds, rerun_on_failure = false, reach = "model" },
      # ONE state, not 24. The fan-out advances every run in lockstep and
      # submits one Message Batch per step (ADR-0020), but it does so inside a
      # single process that holds the wavefront -- each run's world, agent
      # memories and unwritten events -- in memory between steps. Nothing
      # persists a wavefront, so a state per step would have nothing to hand
      # the next one. What the code does persist is completed runs, so the
      # unit of orchestration is the whole phase and the unit of resumption is
      # the run. No heartbeat either: the process has no way to send one, and a
      # batch legitimately stays silent for hours. The stop rule is the timeout.
      { state = "SimulateAll", command = ["cascade", "simulate", "all", "--wave", tostring(var.simulate_wave)], timeout = var.fanout_timeout_seconds, rerun_on_failure = false, reach = "model" },
      { state = "EnsembleCollapse", command = ["cascade", "ensemble", "collapse"], timeout = var.step_timeout_seconds, rerun_on_failure = false, reach = "none" },
      # The same wavefront shape as SimulateAll, over the other eleven cells.
      { state = "EvalGrid", command = ["cascade", "eval", "grid"], timeout = var.fanout_timeout_seconds, rerun_on_failure = false, reach = "model" },
      { state = "Report", command = ["cascade", "report"], timeout = var.step_timeout_seconds, rerun_on_failure = false, reach = "none" },
    ]
  }

  environments = {
    ingest = var.ingest_environment
    study  = var.study_environment
  }

  # What each machine's tasks run as. The ingest is the bench's task; the study
  # is modules/study's when the caller has one, and the bench's otherwise --
  # which cannot call a model, so the chain fails closed at its first state.
  tasks = {
    ingest = {
      task_definition_arn     = var.task_definition_arn
      task_execution_role_arn = var.task_execution_role_arn
      task_role_arn           = var.task_role_arn
      security_group_ids      = var.security_group_ids
    }
    study = var.study_task == null ? {
      task_definition_arn     = var.task_definition_arn
      task_execution_role_arn = var.task_execution_role_arn
      task_role_arn           = var.task_role_arn
      security_group_ids      = var.security_group_ids
    } : var.study_task
  }

  # Whether a phase leaves the isolated subnets. Only with an egress tier to
  # leave to; only for a phase that fetches; and, for a model call, only when
  # the provider has no private path.
  uses_egress = {
    sources = var.egress_network != null
    model   = var.egress_network != null && var.model_calls_use_egress
    none    = false
  }

  # Every state retries a task that never started: ECS refused or throttled
  # the RunTask call, or had no Fargate capacity. No container ran, so nothing
  # was spent and no verdict was issued -- retrying cannot hide one.
  launch_retry = {
    ErrorEquals     = ["ECS.AmazonECSException", "ECS.ServerException"]
    IntervalSeconds = 30
    MaxAttempts     = 4
    BackoffRate     = 2
    MaxDelaySeconds = 600
    JitterStrategy  = "FULL"
  }

  # A task that ran and failed is re-run automatically for `corpus build`
  # alone. Step Functions reports every non-zero exit as States.TaskFailed, so
  # a Retry cannot tell exit 1 from exit 2 -- and that decides who may have it:
  #
  # * `corpus build` spends no model money, resumes by skipping done units, and
  #   its usual failure is a fetch that died mid-stream. Re-running an exit 3
  #   (low disk) is pointless but bounded, and still ends FAILED.
  # * The study's phases are resumable too -- completed runs are skipped -- but
  #   the cost meter starts every process at zero: nothing reads its checkpoint
  #   back. A task that exits 2 at the ceiling and is re-run gets the whole
  #   ceiling again, so N attempts is N ceilings. Until spend is restored on
  #   start, a person re-runs those, by starting a new execution.
  # * The verdict commands fail because the answer is no. Asking again is not
  #   a retry.
  #
  # Bounded, with backoff -- not a loop. `corpus build` stops itself at
  # corpus.max_chunks; the machine must not be the thing that restarts it
  # forever.
  rerun_retry = {
    ErrorEquals     = ["States.TaskFailed"]
    IntervalSeconds = 120
    MaxAttempts     = 3
    BackoffRate     = 2
    MaxDelaySeconds = 1800
    JitterStrategy  = "FULL"
  }

  # The helper: every task state comes from this one expression, so a state
  # cannot exist without its Catch, its timeout or its network configuration.
  definitions = {
    for machine, chain in local.chains : machine => {
      Comment = "Cascade ${machine} chain. Generated by infra/terraform/modules/pipeline; every state is one CLI phase."
      StartAt = chain[0].state
      States = merge(
        {
          for i, step in chain : step.state => {
            Type           = "Task"
            Resource       = local.run_task_sync
            TimeoutSeconds = step.timeout
            Parameters = {
              LaunchType     = "FARGATE"
              Cluster        = var.cluster_arn
              TaskDefinition = local.tasks[machine].task_definition_arn
              NetworkConfiguration = {
                AwsvpcConfiguration = {
                  # The egress tier's subnet INSTEAD of the isolated ones, and
                  # its security group IN ADDITION to the task's own -- which
                  # still carries the database, endpoint and cache rules.
                  Subnets        = local.uses_egress[step.reach] ? var.egress_network.subnet_ids : var.subnet_ids
                  SecurityGroups = concat(local.tasks[machine].security_group_ids, local.uses_egress[step.reach] ? var.egress_network.security_group_ids : [])
                  # Even there. The way out is the NAT gateway; a task never
                  # has an address anyone could reach it on.
                  AssignPublicIp = "DISABLED"
                }
              }
              Overrides = {
                ContainerOverrides = [{
                  Name        = var.container_name
                  Command     = step.command
                  Environment = [for key in sort(keys(local.environments[machine])) : { Name = key, Value = local.environments[machine][key] }]
                }]
              }
            }
            # The result is ECS's description of a stopped task. Nothing reads
            # it, and the exit code already decided which edge was taken.
            ResultPath = null
            Retry      = concat([local.launch_retry], step.rerun_on_failure ? [local.rerun_retry] : [])
            Catch = [{
              ErrorEquals = ["States.ALL"]
              ResultPath  = "$.failure"
              Next        = "${step.state}Failed"
            }]
            Next = i + 1 < length(chain) ? chain[i + 1].state : "Done"
          }
        },
        # A Catch cannot say which state it came from, and by the time the
        # alert is published the context object names the alert state. So each
        # task gets a Pass that records its own name on the way to the alert.
        {
          for step in chain : "${step.state}Failed" => {
            Type = "Pass"
            Parameters = {
              "execution.$" = "$$.Execution.Name"
              "machine.$"   = "$$.StateMachine.Name"
              state         = step.state
              command       = join(" ", step.command)
              "failure.$"   = "$.failure"
            }
            ResultPath = "$.failed"
            Next       = "NotifyFailure"
          }
        },
        {
          NotifyFailure = {
            Type     = "Task"
            Resource = local.sns_publish
            Parameters = {
              TopicArn    = var.alerts_topic_arn
              "Subject.$" = "States.Format('{} failed at {}', $$.StateMachine.Name, $.failed.state)"
              "Message.$" = "States.JsonToString($.failed)"
            }
            ResultPath = null
            Retry = [{
              ErrorEquals     = ["States.TaskFailed"]
              IntervalSeconds = 5
              MaxAttempts     = 3
              BackoffRate     = 2
              MaxDelaySeconds = 60
              JitterStrategy  = "FULL"
            }]
            # An alert that cannot be sent must not turn a failed chain into
            # anything else: both edges out of this state reach Failed.
            Catch = [{
              ErrorEquals = ["States.ALL"]
              ResultPath  = "$.notify_failure"
              Next        = "Failed"
            }]
            Next = "Failed"
          }
          # Fail, never Succeed: a supervisor polling the execution must not be
          # able to read a chain that stopped as a chain that finished.
          Failed = {
            Type      = "Fail"
            Error     = "Cascade.PhaseFailed"
            CausePath = "States.Format('{} failed with {}', $.failed.state, $.failed.failure.Error)"
          }
          Done = {
            Type = "Succeed"
          }
        },
      )
    }
  }

  # IAM scopes derived from the ARNs, so the role follows the inputs.
  task_definition_families = distinct([for machine in sort(keys(local.tasks)) : replace(local.tasks[machine].task_definition_arn, "/:[0-9]+$/", "")])
  passed_role_arns         = distinct(flatten([for machine in sort(keys(local.tasks)) : [local.tasks[machine].task_execution_role_arn, local.tasks[machine].task_role_arn]]))
  cluster_tasks            = "${replace(var.cluster_arn, ":cluster/", ":task/")}/*"
  state_machine_arns       = [for machine in sort(keys(local.chains)) : "arn:${local.partition}:states:${local.region}:${local.account}:stateMachine:${var.name}-${machine}"]
}

# --- Logs -----------------------------------------------------------------------------

# Named under /cascade/ so the platform key's CloudWatch Logs grant, which is
# conditioned on that prefix, covers it.
resource "aws_cloudwatch_log_group" "states" {
  name              = "/cascade/${var.name}/pipeline"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
}

# --- The role both machines run as -----------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
    # Only for these two machines in this account (the confused-deputy guard).
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account]
    }
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = local.state_machine_arns
    }
  }
}

resource "aws_iam_role" "states" {
  name               = "${var.name}-pipeline"
  description        = "Step Functions: run the cascade task, stop it, and announce a failure"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "states" {
  # Any revision of the named families -- the bench's, and the study's when
  # there is one -- in the one cluster: a new image tag is a new revision, and
  # must not need a policy change to run.
  statement {
    sid       = "RunTheCascadeTaskOnly"
    actions   = ["ecs:RunTask"]
    resources = [for family in local.task_definition_families : "${family}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [var.cluster_arn]
    }
  }
  # .sync polls the task it started, and stops it when a state times out or an
  # execution is aborted -- otherwise a stopped execution leaves a task
  # spending.
  statement {
    sid       = "WatchAndStopTasksInThisCluster"
    actions   = ["ecs:DescribeTasks", "ecs:StopTask"]
    resources = [local.cluster_tasks]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [var.cluster_arn]
    }
  }
  # RunTask hands ECS the task's roles. Those, to ECS, and nothing else -- an
  # unrestricted PassRole is how a scheduler becomes an admin.
  statement {
    sid       = "PassTheTaskRolesToEcsOnly"
    actions   = ["iam:PassRole"]
    resources = local.passed_role_arns
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  # .sync learns that a task stopped from an EventBridge rule Step Functions
  # manages under this fixed name.
  statement {
    sid       = "ManageTheSyncRule"
    actions   = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
    resources = ["arn:${local.partition}:events:${local.region}:${local.account}:rule/StepFunctionsGetEventsForECSTaskRule"]
  }
  statement {
    sid       = "AnnounceFailures"
    actions   = ["sns:Publish"]
    resources = [var.alerts_topic_arn]
  }
  # The topic is encrypted, so publishing needs its key -- through SNS only.
  statement {
    sid       = "UseTheTopicKeyThroughSns"
    actions   = ["kms:GenerateDataKey", "kms:Decrypt"]
    resources = [var.kms_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["sns.${local.region}.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "states" {
  name   = "run-and-announce"
  role   = aws_iam_role.states.id
  policy = data.aws_iam_policy_document.states.json
}

# The two statements that cannot name a resource, kept in a document of their
# own so a test can pin exactly which actions are allowed to be there.
data "aws_iam_policy_document" "observability" {
  # The CloudWatch Logs delivery APIs and the X-Ray write APIs define no
  # resource types, so IAM accepts only "*" for them; AWS's own Step Functions
  # logging policy is this statement. Checkov's wildcard checks (CKV_AWS_356,
  # CKV_AWS_111) pass it unsuppressed for the same reason: there is no
  # narrower resource to ask for. What the actions reach is a log delivery and
  # a trace segment -- neither is a path to data.
  statement {
    sid = "DeliverLogs"
    actions = [
      "logs:CreateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:DescribeLogGroups",
      "logs:DescribeResourcePolicies",
      "logs:GetLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:UpdateLogDelivery",
    ]
    resources = ["*"]
  }
  statement {
    sid       = "EmitTraces"
    actions   = ["xray:GetSamplingRules", "xray:GetSamplingTargets", "xray:PutTelemetryRecords", "xray:PutTraceSegments"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "observability" {
  name   = "logs-and-traces"
  role   = aws_iam_role.states.id
  policy = data.aws_iam_policy_document.observability.json
}

# --- The machines ---------------------------------------------------------------------------

resource "aws_sfn_state_machine" "this" {
  #checkov:skip=CKV_AWS_285:History IS logged, at level ALL. This check inspects include_execution_data, which is off on purpose -- see the comment on logging_configuration below.
  for_each = local.definitions

  name       = "${var.name}-${each.key}"
  role_arn   = aws_iam_role.states.arn
  type       = "STANDARD"
  definition = jsonencode(each.value)

  # Every transition, without payloads: a fan-out runs for days, and "which
  # state is it in, since when" should not need the console. The only payload
  # these machines ever carry is ECS's description of a failed task, which
  # repeats the plaintext environment overrides; the alert and the execution
  # history already hold it, and a year-long log does not need a third copy.
  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.states.arn}:*"
    include_execution_data = false
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  # Creation checks that the role can already deliver logs.
  depends_on = [aws_iam_role_policy.states, aws_iam_role_policy.observability]
}
