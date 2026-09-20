variable "name" {
  type = string
}

variable "bucket_suffix" {
  description = "Account id and region, so the bucket name is globally unique."
  type        = string
}

variable "kms_key_arn" {
  description = "CMK the reports are encrypted under (the platform root's key)."
  type        = string
}

variable "report_lock_retention_days" {
  description = <<-EOT
    Object Lock default retention, in days, on every version written here.
    Required, with no default, because it prices a decision this module will
    not make for the caller.

    Why lock a derived artifact at all. A report is not byte-reproducible --
    `manifest.json` carries a timestamp and a git sha -- and more to the
    point, a published report is the study's CLAIM. §1 says that if the true
    Brier lands at 0.168 the report says 0.168; a claim that can be quietly
    withdrawn and replaced is a claim whose history can be edited. A
    correction is published as a new report that supersedes the old one, and
    both remain.

    What it costs, operationally, for the whole of this many days:
      * a report uploaded with a wrong number cannot be removed -- only
        superseded. That is the point, and it is still a cost.
      * the choice is one-way for the bucket's life, not just for the
        retention: AWS states that once Object Lock is enabled "you can't
        disable Object Lock or suspend versioning for that bucket".
      * `terraform destroy` cannot empty this bucket, exactly as it cannot
        empty the trail, the lake or the recovery buckets (see the README).
      * every upload must carry a checksum: S3 requires `Content-MD5` or
        `x-amz-sdk-checksum-algorithm` "for any request to upload an object
        with a retention period configured using Amazon S3 Object Lock". The
        AWS CLI and the SDKs send one; a hand-rolled HTTP PUT would not.
      * GOVERNANCE's escape hatch, `s3:BypassGovernanceRetention`, is denied
        in the bucket policy to every principal, so the escape is two acts and
        both are on the trail.

    ADR-0035's Object Lock lesson does NOT apply here: it is that AWS Config
    cannot deliver to a bucket with a default retention, and nothing delivers
    Config -- or DataSync, ADR-0042's other unverified case -- into this one.
    The only writer is a task running `cascade report` through the CLI.
  EOT
  type        = number

  validation {
    condition     = var.report_lock_retention_days >= 1 && floor(var.report_lock_retention_days) == var.report_lock_retention_days
    error_message = "report_lock_retention_days must be a whole number of days, at least 1."
  }
}

variable "simulation_principal_arns" {
  description = <<-EOT
    IAM principals the simulation runs as. They are denied reading anything
    under the reports prefix: `baselines.csv` and `ablation_grid.csv` carry an
    `outcome` column per scenario, so a report is a label-bearing artifact in
    the same sense the registry archive is (invariant 2).

    The study task role belongs in this list even though it is the writer.
    Write-only is the design: it produces reports and may not read one back.
  EOT
  type        = list(string)
  default     = []
}
