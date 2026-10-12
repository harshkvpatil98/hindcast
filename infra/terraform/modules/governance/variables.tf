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

variable "extra_topic_policy_documents" {
  description = <<-EOT
    Policy documents (JSON) merged into the alerts topic's policy. An SNS topic
    has exactly one policy, and this module's names only the two cost services;
    anything else that alerts here -- alarms, EventBridge rules -- must be
    admitted through this, or its alerts are dropped without an error.
  EOT
  type        = list(string)
  default     = []
}

variable "anomaly_threshold_usd" {
  description = "Absolute impact above which a cost anomaly alerts."
  type        = number
  default     = 10
}

variable "create_budget" {
  description = <<-EOT
    Create the account budget. False only where the account already carries a
    budget made outside Terraform that is to stay the one budget: a second is
    legal, and is a second set of alerts against a different limit.
  EOT
  type        = bool
  default     = true
}

variable "create_anomaly_detection" {
  description = <<-EOT
    Create the per-service cost anomaly monitor and its subscription. Cost
    Anomaly Detection allows one AWS-services monitor per account, and makes
    one itself ("Default-Services-Monitor") when Cost Explorer is first
    enabled. Where that exists CreateAnomalyMonitor is refused at apply, which
    no plan and no mock-provider test can see. False there.
  EOT
  type        = bool
  default     = true
}
