variable "name" {
  description = "Prefix for every resource name."
  type        = string
}

variable "vpc_id" {
  type = string
}

variable "availability_zone" {
  description = <<-EOT
    The one AZ the tier lives in, named explicitly like modules/network's. It
    must be an AZ the isolated tier also uses: a task here mounts the cache
    through the mount target in its own AZ, and there is one only where
    modules/network has a subnet. The root checks that; this module cannot.
  EOT
  type        = string
}

variable "public_subnet_cidr" {
  description = "CIDR for the subnet that holds the NAT gateway. Must not overlap modules/network's subnets."
  type        = string
}

variable "workload_subnet_cidr" {
  description = "CIDR for the subnet egress tasks run in. Must not overlap modules/network's subnets."
  type        = string
}

variable "s3_gateway_endpoint_id" {
  description = "modules/network's S3 gateway endpoint, so in-region S3 stays under its bucket allow-list and off the NAT."
  type        = string
}

variable "allowed_domains" {
  description = <<-EOT
    Every name an egress task may resolve, beyond the regional AWS names this
    module always allows. Required, with no default: what the study may reach
    on the internet is a decision, and it is reviewed by reading this list.
    The ingest's hosts are in cascade/corpus/sources/*.py; the model's, when
    the provider is reached this way, in cascade/llm/providers.py.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.allowed_domains) > 0
    error_message = "allowed_domains must name at least one domain: an egress tier that may reach nothing should not be created."
  }
  # A leading "*." label is how DNS Firewall spells "and its subdomains". A
  # bare "*", or a wildcard over a whole TLD, is the allow-list switched off.
  validation {
    condition     = alltrue([for d in var.allowed_domains : can(regex("^(\\*\\.)?([a-z0-9-]+\\.)+[a-z]{2,}$", d)) && !can(regex("^\\*\\.[a-z]{2,}$", d))])
    error_message = "allowed_domains takes lower-case host names, optionally with one leading \"*.\" label; \"*\" and \"*.<tld>\" are refused."
  }
}

variable "dns_firewall_action" {
  description = <<-EOT
    What happens to a lookup that is not on the allow-list: "ALERT" (answered,
    and logged) or "BLOCK" (NXDOMAIN, and logged). Required, with no default.
    BLOCK is the control; ALERT is how the list is proven complete first --
    DNS Firewall is associated with the whole VPC, so a list that forgot a
    name the isolated tier needs would break the bench and the database tasks,
    and no offline gate can find that out. Choosing either is a decision.
  EOT
  type        = string

  validation {
    condition     = contains(["ALERT", "BLOCK"], var.dns_firewall_action)
    error_message = "dns_firewall_action must be \"ALERT\" or \"BLOCK\"."
  }
}

variable "log_kms_key_arn" {
  description = "CMK for the DNS query log group. Its policy must admit CloudWatch Logs for /cascade/* groups, as the sandbox's does."
  type        = string
}

variable "log_retention_days" {
  description = "Retention for the DNS query log."
  type        = number
  default     = 90
}
