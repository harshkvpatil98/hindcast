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

variable "alerts_topic_arn" {
  description = "Where GuardDuty findings above the threshold go (modules/governance). The root must merge `required_topic_policy_statements_json` into that topic's policy, or they go nowhere -- with no error."
  type        = string

  validation {
    condition     = can(regex("^arn:[^:]+:sns:[^:]+:[0-9]{12}:[^:]+$", var.alerts_topic_arn))
    error_message = "alerts_topic_arn must be an SNS topic ARN."
  }
}

variable "guardduty_min_severity" {
  description = <<-EOT
    The lowest GuardDuty severity that is published to the alerts topic.
    Required, with no default: what pages a person is a decision about this
    account and who is on the other end of the topic. GuardDuty's scale is
    1.0-3.9 Low, 4.0-6.9 Medium, 7.0-8.9 High, 9.0-10.0 Critical.
  EOT
  type        = number

  validation {
    condition     = var.guardduty_min_severity >= 1 && var.guardduty_min_severity <= 10
    error_message = "guardduty_min_severity must be between 1 (every finding) and 10 (only the most critical)."
  }
}
