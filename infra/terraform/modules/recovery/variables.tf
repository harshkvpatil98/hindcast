variable "name" {
  type = string
}

variable "bucket_suffix" {
  description = "Account id, so bucket names are globally unique; each bucket adds its own region."
  type        = string
}

variable "kms_key_arn" {
  description = "CMK in the primary region."
  type        = string
}

variable "simulation_principal_arns" {
  description = <<-EOT
    IAM principals the simulation runs as. They are explicitly denied the
    registry prefix: the archive there holds the resolution labels, and
    invariant 2 says the simulation never reads them -- in the database a grant
    enforces that, here this does.
  EOT
  type        = list(string)
  default     = []
}

variable "retention_days" {
  type    = number
  default = 365
}
