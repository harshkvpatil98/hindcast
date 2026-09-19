variable "name" {
  type = string
}

variable "bucket_suffix" {
  description = "Appended to bucket names so they are globally unique (account id and region)."
  type        = string
}

variable "kms_key_arn" {
  description = <<-EOT
    CMK for the trail's log files, its log group and the Config history. The
    key is the caller's, so its policy is too: it must carry the statements in
    the `required_key_policy_statements_json` output, or CloudTrail refuses to
    create the trail and CloudWatch Logs refuses the log group.
  EOT
  type        = string
}

variable "data_event_bucket_arns" {
  description = <<-EOT
    Buckets whose object-level reads and writes the trail records. The event
    lake and the recovery buckets carry no S3 access logging on purpose; this
    is their access record. The module's own Config bucket is always recorded
    as well, listed here or not.
  EOT
  type        = list(string)
  default     = []

  # A selector is a prefix match on the object ARN. "arn:aws:s3:::bucket/*"
  # would become the literal prefix "bucket/*/", match no object, and record
  # nothing -- with no error from anyone.
  validation {
    condition     = alltrue([for arn in var.data_event_bucket_arns : can(regex("^arn:[^:]+:s3:::[^/*]+$", arn))])
    error_message = "data_event_bucket_arns takes bare bucket ARNs (arn:aws:s3:::name), with no object path or wildcard."
  }
}

variable "retention_days" {
  description = "Object Lock default retention on the trail's log files, in days."
  type        = number
  default     = 365

  validation {
    condition     = var.retention_days >= 1 && floor(var.retention_days) == var.retention_days
    error_message = "retention_days must be a whole number of days, at least 1."
  }
}

variable "log_retention_days" {
  description = "Retention of the trail's CloudWatch log group: the searchable copy, not the record."
  type        = number
  default     = 365

  # CloudWatch Logs accepts only these values, and says so at apply -- which
  # no offline gate reaches.
  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "log_retention_days must be a retention period CloudWatch Logs accepts (1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, ...)."
  }
}
