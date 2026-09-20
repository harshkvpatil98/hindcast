variable "name" {
  type = string
}

variable "allowed_regions" {
  description = "Regions the workload accounts may use. Required: where a study runs is a decision (ADR-0028)."
  type        = list(string)

  validation {
    condition     = length(var.allowed_regions) > 0
    error_message = "allowed_regions must name at least one region."
  }
}

variable "target_ids" {
  description = <<-EOT
    Organizational units or accounts the policies attach to. Empty by default:
    the policies are then created but bind nothing, so the design can be
    reviewed and tested before it can lock anyone out.
  EOT
  type        = set(string)
  default     = []
}
