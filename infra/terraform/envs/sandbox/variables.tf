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

variable "experiment_clone" {
  description = "Create the copy-on-write clone for the partitioning experiment."
  type        = bool
  default     = false
}
