variable "name" {
  description = "Prefix for every resource name."
  type        = string
}

variable "cidr_block" {
  description = "VPC CIDR."
  type        = string
  default     = "10.40.0.0/16"
}

variable "availability_zones" {
  description = <<-EOT
    AZs for the private subnets, named explicitly rather than looked up: a data
    source would make the subnet layout a function of whatever the account
    reports on the day, and Aurora's subnet group needs at least two.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 2
    error_message = "Aurora's DB subnet group needs subnets in at least two availability zones."
  }
}

variable "interface_endpoints" {
  description = <<-EOT
    AWS services reached through interface endpoints. There is no NAT gateway,
    so a service missing here is unreachable from the VPC -- by design.
  EOT
  type        = set(string)
  default     = ["ecr.api", "ecr.dkr", "logs", "secretsmanager"]
}

variable "extra_interface_endpoints" {
  description = <<-EOT
    Services added to `interface_endpoints` by a caller that needs one more --
    the study's model provider, when it has a PrivateLink service (Bedrock:
    "bedrock-mantle"). Separate from the default set so adding one cannot
    silently drop the four every task needs.
  EOT
  type        = set(string)
  default     = []
}

variable "named_interface_endpoints" {
  description = <<-EOT
    Interface endpoints whose service name does not follow
    com.amazonaws.<region>.<key>, as key => full service name. Exists for
    Claude Platform on AWS: Anthropic's documentation states PrivateLink is
    supported, but the endpoint service name is published on no page this
    project could read, so it is an input and never a guess (ADR-0042).
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for name in values(var.named_interface_endpoints) : can(regex("^[a-z0-9.-]+\\.[a-z0-9.-]+$", name))])
    error_message = "named_interface_endpoints values must be full VPC endpoint service names, e.g. com.amazonaws.us-east-1.example."
  }
}

variable "s3_allowed_bucket_arns" {
  description = <<-EOT
    Buckets the S3 gateway endpoint may reach. ECR stores image layers in an
    AWS-owned bucket, which is always added; everything else is named here, so
    the endpoint cannot be used to move data to an arbitrary bucket.
  EOT
  type        = list(string)
  default     = []
}

variable "log_kms_key_arn" {
  description = "CMK for the flow-log group."
  type        = string
}

variable "flow_log_retention_days" {
  description = "Retention for VPC flow logs."
  type        = number
  default     = 90
}
