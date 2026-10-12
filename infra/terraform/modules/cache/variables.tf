variable "name" {
  description = "Prefix for every resource name, and the cache's directory under the recovery prefix."
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  description = "The isolated tier's subnets (modules/network): one mount target in each. DataSync's interfaces go in the first."
  type        = list(string)

  validation {
    condition     = length(var.subnet_ids) > 0
    error_message = "At least one subnet is required for a mount target."
  }
}

variable "kms_key_arn" {
  description = "CMK for the file system and the sync task's log group (the sandbox's platform key)."
  type        = string
}

variable "client_security_group_ids" {
  description = "Security groups allowed to reach the cache over NFS, as label => id. Nothing else is, except this module's own DataSync group."
  type        = map(string)
  default     = {}
}

variable "recovery_bucket_arn" {
  description = "The tier-0 recovery bucket (modules/recovery, in the platform root). An ARN, not a name: the role's S3 permissions are scoped from it."
  type        = string

  validation {
    condition     = can(regex("^arn:[^:]+:s3:::[^/*]+$", var.recovery_bucket_arn))
    error_message = "recovery_bucket_arn must be a bare bucket ARN (arn:aws:s3:::name)."
  }
}

variable "recovery_prefix" {
  description = "The recovery bucket's prefix for LLM cache archives, from modules/recovery's output -- passed, not restated, so the two cannot drift."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.recovery_prefix))
    error_message = "recovery_prefix is one path segment: lower-case letters, digits and hyphens, no slashes."
  }
}

variable "recovery_kms_key_arn" {
  description = "The CMK the recovery bucket is encrypted under (the platform root's key)."
  type        = string
}

variable "sync_schedule_expression" {
  description = <<-EOT
    How often the cache is copied to the recovery bucket, e.g. "rate(1 hour)".
    Required, with no default, because it prices two things nobody has
    measured against each other. It is the RPO for losing the file system:
    every recording made since the last run is paid for again. And every run
    lists the whole archive -- S3 bills a request per object scanned even when
    nothing moves -- so its cost grows with the cache, which at study scale is
    hundreds of thousands of entries. An hour is DataSync's minimum interval.
  EOT
  type        = string

  validation {
    condition     = can(regex("^(rate\\([1-9][0-9]* (hour|hours|day|days)\\)|cron\\(.+\\))$", var.sync_schedule_expression))
    error_message = "sync_schedule_expression must be rate(N hours|days) or cron(...); DataSync does not schedule more often than hourly."
  }
}

variable "alerts_topic_arn" {
  description = "Where a failed sync is announced (the platform root's alerts topic), or null for nowhere. A sandbox can exist before that root does."
  type        = string
  default     = null
}

variable "log_retention_days" {
  type    = number
  default = 30
}
