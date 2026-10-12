variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr_block" {
  type = string
}

variable "s3_prefix_list_id" {
  description = "The S3 gateway endpoint's prefix list, for egress to S3."
  type        = string
}

variable "db_security_group_id" {
  type = string
}

variable "db_port" {
  type = number
}

variable "db_user_arns" {
  description = "rds-db:connect resources for the application roles (modules/bench)."
  type        = list(string)
}

variable "container_definition" {
  description = "modules/bench's container, as an object. This module overrides its LLM settings, adds the cache mount, and restates nothing else."
  type        = any
}

variable "execution_role_arn" {
  description = "modules/bench's execution role: pulls the same image, writes the same log group, resolves the same database secrets."
  type        = string
}

variable "execution_role_name" {
  description = "The same role by name, to attach the one extra secret the anthropic provider needs."
  type        = string
}

variable "cache" {
  description = "The durable LLM cache (modules/cache)."
  type = object({
    file_system_id    = string
    file_system_arn   = string
    access_point_id   = string
    access_point_arn  = string
    security_group_id = string
  })
}

variable "model_provider" {
  description = <<-EOT
    Who serves the model: "anthropic", "aws" (Claude Platform on AWS) or
    "bedrock". Required, with no default: it decides where the study's largest
    line item is billed, which IAM service authorizes it, and whether these
    tasks need a path to the internet. `claude_code` is not offered -- it is a
    local subscription login, excluded from every AWS path (ADR-0031).
  EOT
  type        = string

  validation {
    condition     = contains(["anthropic", "aws", "bedrock"], var.model_provider)
    error_message = "model_provider must be \"anthropic\", \"aws\" or \"bedrock\"."
  }
}

variable "model_region" {
  description = "Region of the aws workspace or the bedrock endpoint. Explicit, never inherited from the task's own region (ADR-0028). Unused by anthropic."
  type        = string
  default     = null

  validation {
    condition     = var.model_provider == "anthropic" || var.model_region != null
    error_message = "model_region is required for the aws and bedrock providers: where the model is called is where its spend lands (ADR-0028)."
  }
}

variable "workspace_id" {
  description = "Claude Platform on AWS workspace (wrkspc_...). Not a credential. The task role may call this workspace and no other."
  type        = string
  default     = null

  validation {
    condition     = var.model_provider != "aws" || var.workspace_id != null
    error_message = "workspace_id is required for the aws provider: the IAM grant is scoped to one workspace, and the service refuses a request without the header."
  }
}

variable "api_key_secret" {
  description = <<-EOT
    For the anthropic provider: the Secrets Manager secret holding the API key,
    and the CMK it is encrypted under. Created and filled outside Terraform, so
    the key is never in a plan, a state file or a variable.
  EOT
  type = object({
    arn         = string
    kms_key_arn = string
  })
  default = null

  validation {
    condition     = var.model_provider != "anthropic" || var.api_key_secret != null
    error_message = "api_key_secret is required for the anthropic provider: the key arrives as an ECS secret or not at all."
  }
}

variable "task_cpu" {
  type = number
}

variable "task_memory" {
  type = number
}

variable "ephemeral_storage_gib" {
  description = "Scratch. The cache no longer lives here, so this holds checkpoints and reports only."
  type        = number
  default     = 21
}

variable "reports" {
  description = <<-EOT
    Where this sandbox publishes what `cascade report` writes (ADR-0046) --
    the platform root's `reports_publish` output, passed whole, so the two
    roots cannot spell the prefix differently.

    Required, like `cache`. A study task that cannot publish reproduces the
    gap this closes: the deliverable existed only in a task's scratch storage,
    which dies with the task.

    There is deliberately no environment variable for the destination. The CLI
    writes `reports/study_<ts>/` locally and the upload is one
    `aws s3 cp --recursive` (the sandbox root prints it); a `CASCADE_`-prefixed
    variable the settings do not define is a validation error by design, so
    inventing one here would stop the task rather than configure it.
  EOT
  type = object({
    bucket_arn  = string
    kms_key_arn = string
    prefix      = string
  })

  validation {
    condition     = can(regex("^arn:[^:]+:s3:::[^/*]+$", var.reports.bucket_arn))
    error_message = "reports.bucket_arn must be a bare bucket ARN (arn:aws:s3:::name)."
  }

  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.reports.prefix))
    error_message = "reports.prefix is one path segment: lower-case letters, digits and hyphens, no slashes."
  }
}
