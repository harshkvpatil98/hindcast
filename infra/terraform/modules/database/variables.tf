variable "name" {
  description = "Cluster identifier and resource-name prefix."
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "Private subnets, in at least two AZs."
  type        = list(string)
}

variable "client_security_group_ids" {
  description = "Security groups allowed to reach PostgreSQL. Nothing else is."
  type        = map(string)
  default     = {}
}

variable "engine_version" {
  description = <<-EOT
    Aurora PostgreSQL minor version. Pinned, with automatic minor upgrades off,
    because the minor version decides the pgvector version: an upgrade from
    16.11 to 16.13 moves pgvector 0.8.0 -> 0.8.1 under a benchmark that is
    meant to compare against the local 0.8.0 measurements (M8).
  EOT
  type        = string
  default     = "16.11"

  # The M11 hard gate, as a mechanism. The schema uses halfvec(384) (pgvector
  # >= 0.7) and HNSW with ef_search; cascade doctor pins 0.8. Versions and
  # their pgvector releases from the AWS Aurora PostgreSQL extension table,
  # read 2026-09-19: 16.8-16.11 -> 0.8.0, 16.13 -> 0.8.1, 16.14 -> 0.8.2.
  validation {
    condition     = contains(["16.8", "16.9", "16.10", "16.11", "16.13", "16.14"], var.engine_version)
    error_message = "Only Aurora PostgreSQL 16.x minors that ship pgvector >= 0.8.0 are allowed (16.8-16.11, 16.13, 16.14). Anything older lacks what the schema needs; see ADR-0034."
  }
}

variable "min_acu" {
  description = "Serverless v2 minimum capacity (ACUs; 1 ACU ~ 2 GiB of memory)."
  type        = number
}

variable "max_acu" {
  description = "Serverless v2 maximum capacity."
  type        = number

  validation {
    condition     = var.max_acu >= 1 && var.max_acu <= 256
    error_message = "max_acu must be between 1 and 256."
  }
}

variable "instance_count" {
  description = "Writer plus readers."
  type        = number
  default     = 1
}

variable "database_name" {
  type    = string
  default = "cascade"
}

variable "master_username" {
  description = "Matches CASCADE_DATABASE__ADMIN_USER; the migrations run as this role."
  type        = string
  default     = "cascade_admin"
}

variable "app_roles" {
  description = <<-EOT
    Database roles the migrations create (001), each given a generated password
    in Secrets Manager. The simulation role and the evaluation role are
    separate so invariant 2 -- the simulation never reads scenario_labels --
    stays a Postgres grant on Aurora exactly as it is locally.
  EOT
  type        = set(string)
  default     = ["cascade_sim", "cascade_eval"]
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "skip_final_snapshot" {
  type    = bool
  default = false
}

variable "backup_retention_days" {
  type    = number
  default = 7
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "experiment_clone" {
  description = <<-EOT
    Create a copy-on-write clone of the cluster for the partitioning experiment
    M8 deferred (quarterly vs annual). A clone shares storage pages with the
    source until either writes, so re-partitioning 1.95M rows on the clone
    neither disturbs the baseline nor pays for a second full copy.
  EOT
  type        = bool
  default     = false
}
