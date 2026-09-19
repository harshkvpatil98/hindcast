variable "name" {
  description = "Prefix for the two state machines, their role and their log group."
  type        = string

  # The alert's Subject is "<name>-<machine> failed at <State>", and SNS
  # refuses a Subject of 100 characters or more. A refused publish on the
  # failure path is a lost alert, so the bound is enforced where it is cheap.
  validation {
    condition     = can(regex("^[A-Za-z0-9_-]{1,40}$", var.name))
    error_message = "name must be 1-40 characters of letters, digits, hyphen or underscore."
  }
}

variable "cluster_arn" {
  description = "ECS cluster the tasks run in (modules/bench). An ARN, not a name: the role's task permissions are scoped from it."
  type        = string

  validation {
    condition     = can(regex(":cluster/[^/]+$", var.cluster_arn))
    error_message = "cluster_arn must be an ECS cluster ARN."
  }
}

variable "task_definition_arn" {
  description = <<-EOT
    The task definition every phase runs as (modules/bench), WITH its revision.
    A revision pins what an execution runs the way an immutable image tag pins
    what the revision runs; a bare family would mean "whatever is newest".
  EOT
  type        = string

  validation {
    condition     = can(regex(":task-definition/[^:/]+:[0-9]+$", var.task_definition_arn))
    error_message = "task_definition_arn must be a task definition ARN ending in :<revision>."
  }
}

variable "task_execution_role_arn" {
  description = "The task definition's execution role. Step Functions may pass this role and the task role to ECS, and no other."
  type        = string
}

variable "task_role_arn" {
  description = "The task definition's task role."
  type        = string
}

variable "container_name" {
  description = "Container whose command each state overrides. Must match the task definition."
  type        = string
  default     = "cascade"
}

variable "subnet_ids" {
  description = "Private subnets for the tasks. No task is ever given a public address."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) > 0
    error_message = "At least one subnet is required."
  }
}

variable "security_group_ids" {
  type = list(string)

  validation {
    condition     = length(var.security_group_ids) > 0
    error_message = "At least one security group is required."
  }
}

variable "alerts_topic_arn" {
  description = "Where a failed chain is announced (modules/governance)."
  type        = string
}

variable "kms_key_arn" {
  description = <<-EOT
    Platform CMK. Encrypts the state machines' log group, and is the key the
    alerts topic is encrypted under -- so the role may use it, through SNS
    only, or the publish on the failure path is denied by KMS.
  EOT
  type        = string
}

variable "log_retention_days" {
  type    = number
  default = 365
}

variable "fanout_timeout_seconds" {
  description = <<-EOT
    Stop rule for the two wavefront phases (`simulate all`, `eval grid`).
    Required, with no default: their wall-clock is set by the provider's batch
    turnaround times 24 x ceil(runs / wave) submissions (ADR-0020), and no
    batch has ever been submitted by this project. A default here would be an
    invented number. On timeout the task is stopped, the chain alerts and
    fails, and the next execution resumes by set difference.
  EOT
  type        = number

  validation {
    condition     = var.fanout_timeout_seconds > 0 && floor(var.fanout_timeout_seconds) == var.fanout_timeout_seconds
    error_message = "fanout_timeout_seconds must be a positive whole number."
  }
}

variable "build_timeout_seconds" {
  description = <<-EOT
    Stop rule for `corpus build`, per attempt. The work is bounded by
    corpus.max_chunks; measured ingest throughput was 28-93 chunks/s locally,
    which puts 1.95M chunks between 6 and 19 hours. Three days is a stop for a
    hung task, not an estimate.
  EOT
  type        = number
  default     = 259200
}

variable "step_timeout_seconds" {
  description = "Stop rule for every other phase: index passes, verdict commands, the collapse, the estimate and the report."
  type        = number
  default     = 21600
}

variable "simulate_wave" {
  description = <<-EOT
    Runs advanced in lockstep by `simulate all` -- ADR-0020's operator dial.
    On Fargate it is also the unit of re-payment: the LLM cache lives on the
    task's scratch volume, so a task that dies mid-wave has paid for answers
    the next task cannot read. `eval grid` keeps the CLI's own default.
  EOT
  type        = number
  default     = 200
}

variable "estimate_units" {
  description = "Sample runs `simulate estimate` measures before extrapolating (spec 12.4)."
  type        = number
  default     = 20
}

variable "ingest_environment" {
  description = "Environment overrides for every ingest task, e.g. CASCADE_CORPUS__MAX_CHUNKS. Plaintext in the definition: never a secret."
  type        = map(string)
  default     = {}
}

variable "study_environment" {
  description = <<-EOT
    Environment overrides for every study task. Plaintext in the definition:
    never a secret. modules/bench pins CASCADE_LLM__MODE=replay because the
    bench never calls a model; a study that should reach one must set
    CASCADE_LLM__MODE=record and the provider routing (ADR-0028) here. Left
    empty, the estimate exits 4 on its first cache miss and the chain fails
    closed, having spent nothing.
  EOT
  type        = map(string)
  default     = {}
}
