variable "region" {
  description = "AWS region. Required, never defaulted (ADR-0028)."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.region))
    error_message = "region must look like an AWS region, e.g. us-east-1."
  }
}

variable "availability_zones" {
  description = "At least two AZs in the region, named explicitly."
  type        = list(string)
}

variable "name" {
  description = "Environment name and resource prefix."
  type        = string
  default     = "cascade-sandbox"
}

variable "acu" {
  description = <<-EOT
    Fixed Aurora capacity for the benchmark: minimum and maximum are both set
    to this. Serverless v2 scales under load, and a p95 measured while it
    scales measures the scaler, not the query. 1 ACU is ~2 GiB of memory; the
    HNSW indexes alone were 1.8 GB at 1.95M chunks (ADR-0026). The M11
    experiment runs at two sizes, set here per run -- never tuned toward a
    target.
  EOT
  type        = number
  default     = 8

  validation {
    condition     = var.acu >= 1 && var.acu <= 256
    error_message = "acu must be between 1 and 256."
  }
}

variable "engine_version" {
  description = "Aurora PostgreSQL minor; the database module refuses any without pgvector >= 0.8.0."
  type        = string
  default     = "16.11"
}

variable "image_tag" {
  type    = string
  default = "m11"
}

variable "pipeline" {
  description = <<-EOT
    Step Functions chains for the ingest and the study, or null for none. It
    lives in this root, beside the task definition and the network it runs in,
    so every input is a module output rather than a value copied between
    roots. `fanout_timeout_seconds` has no default: no batch has ever been
    submitted, so any number would be invented.
  EOT
  type = object({
    alerts_topic_arn       = string
    fanout_timeout_seconds = number
  })
  default = null
}

variable "observability" {
  description = <<-EOT
    Alerts and a dashboard for this sandbox, or null for none. All-or-nothing on
    purpose: the two thresholds depend on measurements nobody has made yet (how
    much memory the bench leaves free, how many connections it opens), so they
    have no default -- and a sandbox can be created before the platform root
    that owns the alerts topic exists.
  EOT
  type = object({
    alerts_topic_arn          = string
    freeable_memory_low_bytes = number
    database_connections_high = number
  })
  default = null
}

variable "experiment_clone" {
  description = "Create the copy-on-write clone for the partitioning experiment."
  type        = bool
  default     = false
}
