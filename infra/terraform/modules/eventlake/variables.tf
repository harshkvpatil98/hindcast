variable "name" {
  type = string
}

variable "bucket_suffix" {
  description = "Appended to bucket names so they are globally unique (account id and region)."
  type        = string
}

variable "kms_key_arn" {
  type = string
}

variable "retention_mode" {
  description = <<-EOT
    GOVERNANCE lets a specially-permitted principal shorten a lock, which is
    what a sandbox needs to be torn down. COMPLIANCE lets nobody, not even the
    account root, until the period ends -- right for a published study, and a
    bucket you cannot delete if you chose it by accident.
  EOT
  type        = string
  default     = "GOVERNANCE"

  validation {
    condition     = contains(["GOVERNANCE", "COMPLIANCE"], var.retention_mode)
    error_message = "retention_mode must be GOVERNANCE or COMPLIANCE."
  }
}

variable "retention_days" {
  type    = number
  default = 365
}

variable "query_scan_limit_bytes" {
  description = "Athena refuses any single query that would scan more than this. 10 GiB by default."
  type        = number
  default     = 10737418240
}

variable "enable_object_lock" {
  description = <<-EOT
    Object Lock on the events bucket, with the default retention above. On by
    default: it is one of invariant 6's two controls here. Off, objects can be
    deleted by anyone the bucket's IAM allows, which is what lets a
    demonstration deployment be torn down. The provider treats the bucket's
    lock setting as immutable, so changing this later replaces the bucket:
    decide before the lake holds anything that matters.
  EOT
  type        = bool
  default     = true
}

variable "force_destroy" {
  description = "Let `terraform destroy` empty the events bucket, every version included. Refused while Object Lock is on: a locked version cannot be removed, so the destroy would fail halfway."
  type        = bool
  default     = false

  validation {
    condition     = !(var.force_destroy && var.enable_object_lock)
    error_message = "force_destroy needs enable_object_lock = false: Object Lock exists to make exactly that deletion fail."
  }
}
