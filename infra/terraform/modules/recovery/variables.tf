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

variable "restore_targets" {
  description = <<-EOT
    Where a restore drill may write (ADR-0046). `bucket_arns` are the bare
    bucket ARNs a restore lands in -- in a drill into an empty account, the
    new sandbox's artifacts bucket -- and `kms_key_arns` the CMKs those are
    encrypted under.

    Empty is the default and the narrowest setting: with no target the restore
    role is strictly read-only and the drill restores to a machine, which is
    where this project's one real loss happened. A recovery bucket cannot
    appear here; naming one is refused at plan.
  EOT
  type = object({
    bucket_arns  = list(string)
    kms_key_arns = list(string)
  })
  default = { bucket_arns = [], kms_key_arns = [] }

  validation {
    condition     = alltrue([for arn in var.restore_targets.bucket_arns : can(regex("^arn:[^:]+:s3:::[^/*]+$", arn))])
    error_message = "restore_targets.bucket_arns takes bare bucket ARNs (arn:aws:s3:::name), with no object path or wildcard."
  }
}

variable "inventory_schedule" {
  description = <<-EOT
    How often S3 writes an inventory of the tier-0 archive: "Daily" or
    "Weekly", the only two frequencies S3 Inventory offers. Required, with no
    default, because it is a schedule and it prices two things against each
    other that nobody here has measured.

    It is what a scheduled upload of the source cache would have been for.
    That upload cannot be scheduled from AWS -- the data is on a person's
    machine, and the cache that matters is the one the sealed split was
    derived from at the moment it was derived (ADR-0046). What CAN be
    scheduled is the check: this report is how "is the sealed split's source
    cache archived, and how many objects does it hold?" is answered without a
    listing and without reading an object.

    Against that: S3 bills an inventory per million objects listed, on every
    run, and the LLM cache at study scale is hundreds of thousands of entries.
    Weekly is the cheaper reading of a dataset that changes when a person
    seals a split; Daily is the reading that notices an omission within a day.
    Neither price has been measured from this account -- the S3 pricing table
    did not render when this was written, so no figure is transcribed here.
  EOT
  type        = string

  validation {
    condition     = contains(["Daily", "Weekly"], var.inventory_schedule)
    error_message = "inventory_schedule must be \"Daily\" or \"Weekly\": S3 Inventory offers no other frequency."
  }
}

variable "inventory_retention_days" {
  description = <<-EOT
    How long a delivered inventory report is kept. Defaulted, unlike the
    schedule, because it is not a decision about what is retained: each report
    supersedes the last, and the archive it describes is the thing under
    Object Lock. AWS's own guidance is to expire old inventory lists.
  EOT
  type        = number
  default     = 90

  validation {
    condition     = var.inventory_retention_days >= 1 && floor(var.inventory_retention_days) == var.inventory_retention_days
    error_message = "inventory_retention_days must be a whole number of days, at least 1."
  }
}
