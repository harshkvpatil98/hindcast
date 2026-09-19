variable "name" {
  type = string
}

variable "study_config_path" {
  description = "Path to configs/base.yaml: the phase ceilings are read from it, never restated here."
  type        = string
}

variable "infrastructure_allowance_usd" {
  description = <<-EOT
    Monthly allowance for everything that is not model spend (Aurora, endpoints,
    Fargate, storage). Required, with no default: it is a decision about this
    account, and an invented number here would be a target nobody measured.
  EOT
  type        = number

  validation {
    condition     = var.infrastructure_allowance_usd > 0
    error_message = "infrastructure_allowance_usd must be positive."
  }
}

variable "kms_key_arn" {
  description = "CMK for the alerts topic."
  type        = string
}

variable "alert_emails" {
  description = "Addresses subscribed to the alerts topic. Each must confirm the subscription."
  type        = set(string)
  default     = []
}

variable "anomaly_threshold_usd" {
  description = "Absolute impact above which a cost anomaly alerts."
  type        = number
  default     = 10
}
