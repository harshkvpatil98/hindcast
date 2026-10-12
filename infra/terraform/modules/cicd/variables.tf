variable "name" {
  type = string
}

variable "github_repository" {
  description = "owner/repo allowed to assume these roles. Exact: no wildcards."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be exactly owner/repo, with no wildcard: a pattern like owner/* would let every repository under that owner deploy here."
  }
}

variable "apply_environment" {
  description = <<-EOT
    GitHub deployment environment whose jobs may assume the apply role. The
    trust is on the environment, not the branch, because an environment can
    require a reviewer: a push to main alone must not be able to change
    infrastructure.
  EOT
  type        = string
  default     = "production"
}

variable "apply_policy_arns" {
  description = <<-EOT
    Managed policies the apply role carries. Required, with no default: what a
    pipeline may change in this account is a decision, and the easy default --
    AdministratorAccess -- is the one this variable exists to make somebody
    choose on purpose.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.apply_policy_arns) > 0
    error_message = "apply_policy_arns must name at least one policy."
  }
}

variable "state_bucket_arn" {
  type = string
}

variable "state_kms_key_arn" {
  type = string
}
