variable "name" {
  type = string
}

variable "alerts_topic_arn" {
  description = <<-EOT
    The alerts topic (modules/governance). Created there, only named here: this
    module adds publishers, and what the topic and its key must allow for them
    is an output, not a second policy resource fighting the first.
  EOT
  type        = string

  validation {
    condition     = can(regex("^arn:[^:]+:sns:[^:]+:[0-9]{12}:.+$", var.alerts_topic_arn))
    error_message = "alerts_topic_arn must be an SNS topic ARN."
  }
}

variable "cluster_arn" {
  description = <<-EOT
    ARN of the ECS cluster the tasks run in. The ARN, not the name: task
    state-change events carry `clusterArn`, and a pattern holding a name matches
    nothing -- an alert rule that can never fire, with no error anywhere.
  EOT
  type        = string

  validation {
    condition     = can(regex("^arn:[^:]+:ecs:[^:]+:[0-9]{12}:cluster/.+$", var.cluster_arn))
    error_message = "cluster_arn must be an ECS cluster ARN (arn:...:cluster/NAME), not a cluster name."
  }
}

variable "db_cluster_identifier" {
  description = "Aurora cluster identifier (modules/database `cluster_identifier`)."
  type        = string
}

variable "min_acu" {
  description = <<-EOT
    The cluster's Serverless v2 minimum capacity. Asked for because it decides
    which capacity alarms mean anything: the bench pins minimum = maximum
    (envs/sandbox `acu`), and on a pinned cluster capacity IS the maximum for
    as long as the cluster exists.
  EOT
  type        = number

  validation {
    condition     = var.min_acu >= 0 && var.min_acu <= var.max_acu
    error_message = "min_acu must be between 0 and max_acu."
  }
}

variable "max_acu" {
  description = "The cluster's Serverless v2 maximum capacity: the threshold of the capacity alarm, read from the caller and never restated."
  type        = number

  validation {
    condition     = var.max_acu >= 1 && var.max_acu <= 256
    error_message = "max_acu must be between 1 and 256."
  }
}

variable "task_log_group_name" {
  description = "Log group the task writes to (modules/bench `log_group`). It must exist: a metric filter cannot be attached to a group that does not."
  type        = string
}

variable "state_machine_arns" {
  description = <<-EOT
    Step Functions state machines to watch. Empty today: the ingest and
    simulation fan-outs are designed, not written. Alarms are keyed by position
    so the list may hold ARNs not yet known at plan time; append to it, because
    reordering replaces alarms.
  EOT
  type        = list(string)
  default     = []
}

# --- Thresholds ---------------------------------------------------------------------
#
# Two kinds. A percentage has a ceiling AWS defines, so "close to it" can be
# defaulted. A byte count or a connection count is only right relative to a
# workload, and nobody has measured this one on Aurora (well-architected.md:
# "No Aurora number exists"), so those are required and have no default.

variable "acu_utilization_high_percent" {
  description = <<-EOT
    ACUUtilization is capacity / max_acu, bounded at 100 by definition, and the
    Aurora guidance is to raise the maximum when it "approaches 100". 90 is that
    sentence as a number, not a measurement of this study.
  EOT
  type        = number
  default     = 90

  validation {
    condition     = var.acu_utilization_high_percent > 0 && var.acu_utilization_high_percent <= 100
    error_message = "acu_utilization_high_percent is a percentage in (0, 100]."
  }
}

variable "cpu_utilization_high_percent" {
  description = <<-EOT
    On Serverless v2, CPUUtilization is CPU used / CPU available at max_acu, so
    it too is bounded at 100 by definition. Defaulted for the same reason as
    acu_utilization_high_percent.
  EOT
  type        = number
  default     = 90

  validation {
    condition     = var.cpu_utilization_high_percent > 0 && var.cpu_utilization_high_percent <= 100
    error_message = "cpu_utilization_high_percent is a percentage in (0, 100]."
  }
}

variable "freeable_memory_low_bytes" {
  description = <<-EOT
    FreeableMemory floor, in bytes. Required, with no default: the right floor
    depends on the capacity and on what the HNSW indexes leave free at it, which
    is exactly what the Aurora bench has not yet measured. An invented number
    here would be a target nobody measured.
  EOT
  type        = number

  validation {
    condition     = var.freeable_memory_low_bytes > 0
    error_message = "freeable_memory_low_bytes must be positive."
  }
  # 1 ACU is ~2 GiB. A floor at or above everything the cluster has would sit
  # in ALARM permanently -- most likely a value given in the wrong unit.
  validation {
    condition     = var.freeable_memory_low_bytes < var.max_acu * 2 * 1024 * 1024 * 1024
    error_message = "freeable_memory_low_bytes must be below the cluster's memory at max_acu (~2 GiB per ACU)."
  }
}

variable "database_connections_high" {
  description = <<-EOT
    DatabaseConnections ceiling. Required, with no default: it depends on
    max_connections at the chosen capacity and on how many workers a phase
    opens, and neither has been measured on Aurora.
  EOT
  type        = number

  validation {
    condition     = var.database_connections_high > 0 && floor(var.database_connections_high) == var.database_connections_high
    error_message = "database_connections_high must be a positive whole number."
  }
}

variable "sustained_minutes" {
  description = <<-EOT
    Consecutive one-minute datapoints a capacity condition must hold before it
    alerts. A debounce, not a measurement: an index build spikes for seconds,
    and the question these alarms answer is whether a whole measurement window
    was capacity-bound.
  EOT
  type        = number
  default     = 5

  validation {
    condition     = var.sustained_minutes >= 1 && floor(var.sustained_minutes) == var.sustained_minutes
    error_message = "sustained_minutes must be a whole number of at least 1."
  }
}
