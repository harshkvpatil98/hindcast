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
