variable "region" {
  description = "AWS region. Required, never defaulted (ADR-0028)."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]$", var.region))
    error_message = "region must look like an AWS region, e.g. us-east-1."
  }
}

variable "availability_zones" {
  description = "At least two AZs in the region, named explicitly."
  type        = list(string)
}

variable "name" {
  description = "Environment name and resource prefix."
  type        = string
  default     = "cascade-sandbox"
}

variable "acu" {
  description = <<-EOT
    Fixed Aurora capacity for the benchmark: minimum and maximum are both set
    to this. Serverless v2 scales under load, and a p95 measured while it
    scales measures the scaler, not the query. 1 ACU is ~2 GiB of memory; the
    HNSW indexes alone were 1.8 GB at 1.95M chunks (ADR-0026). The M11
    experiment runs at two sizes, set here per run -- never tuned toward a
    target.
  EOT
  type        = number
  default     = 8

  validation {
    condition     = var.acu >= 1 && var.acu <= 256
    error_message = "acu must be between 1 and 256."
  }
}

variable "engine_version" {
  description = "Aurora PostgreSQL minor; the database module refuses any without pgvector >= 0.8.0."
  type        = string
  default     = "16.11"
}

variable "image_tag" {
  type    = string
  default = "m11"
}

variable "pipeline" {
  description = <<-EOT
    Step Functions chains for the ingest and the study, or null for none. It
    lives in this root, beside the task definition and the network it runs in,
    so every input is a module output rather than a value copied between
    roots. `fanout_timeout_seconds` has no default: no batch has ever been
    submitted, so any number would be invented.
  EOT
  type = object({
    alerts_topic_arn       = string
    fanout_timeout_seconds = number
  })
  default = null
}

variable "egress" {
  description = <<-EOT
    The egress tier (modules/egress), or null for none -- and null is the
    default, because the default sandbox is ADR-0034's isolated VPC. Setting it
    gives the VPC an internet gateway, one NAT gateway in one AZ, and one
    subnet whose tasks can reach the names on `allowed_domains`; the ingest's
    fetching state runs there, and the study's model calls when the provider
    has no private path. Every attribute is required: which AZ bills the NAT,
    what may be reached, and whether an unlisted name is logged ("ALERT") or
    refused ("BLOCK") are decisions, not constants.
  EOT
  type = object({
    availability_zone   = string
    allowed_domains     = list(string)
    dns_firewall_action = string
  })
  default = null

  # The cache's mount targets exist only where the isolated tier has a subnet,
  # and a task mounts through the one in its own AZ.
  validation {
    condition     = var.egress == null || contains(var.availability_zones, var.egress.availability_zone)
    error_message = "egress.availability_zone must be one of availability_zones: an egress task mounts the LLM cache through the mount target in its own AZ."
  }
}

variable "study" {
  description = <<-EOT
    The study task (modules/study) and its durable LLM cache (modules/cache),
    or null for none. `recovery` is the platform root's `recovery` output,
    passed whole: the cache is copied into that bucket on
    `sync_schedule_expression`, which has no default -- it is the RPO for
    losing the file system, priced against a per-run cost that grows with the
    cache. `model_endpoint_service_name` is the PrivateLink service for Claude
    Platform on AWS, when the operator has it; this project could not find it
    published, so it is never guessed.
  EOT
  type = object({
    model_provider              = string
    model_region                = optional(string)
    workspace_id                = optional(string)
    model_endpoint_service_name = optional(string)
    api_key_secret = optional(object({
      arn         = string
      kms_key_arn = string
    }))
    recovery = object({
      bucket_arn       = string
      kms_key_arn      = string
      llm_cache_prefix = string
    })
    # The platform root's `reports_publish` output, passed whole: where this
    # sandbox publishes what `cascade report` writes (ADR-0046). Required for
    # the same reason `recovery` is -- a study whose deliverable has nowhere
    # to go leaves it in scratch storage that dies with the task.
    reports = object({
      bucket_arn  = string
      kms_key_arn = string
      prefix      = string
    })
    sync_schedule_expression = string
  })
  default = null

  # api.anthropic.com has no private path, and Claude Platform on AWS has one
  # only when its endpoint service is named. Without the egress tier those
  # calls would time out inside a task that is already billing.
  validation {
    condition = (
      var.study == null ? true :
      var.study.model_provider == "anthropic" ? var.egress != null :
      var.study.model_provider == "aws" && var.study.model_endpoint_service_name == null ? var.egress != null :
      true
    )
    error_message = "This provider is reached over the internet: set `egress` as well, or (for aws) give model_endpoint_service_name."
  }
  # An interface endpoint serves its own region.
  validation {
    condition = (
      var.study == null ? true :
      var.study.model_provider == "bedrock" || (var.study.model_provider == "aws" && var.study.model_endpoint_service_name != null) ? var.study.model_region == var.region :
      true
    )
    error_message = "A provider reached through an interface endpoint must be in this sandbox's region: model_region must equal region."
  }
  # ADR-0028: Bedrock has no Message Batches API, and the fan-out fits its
  # ceiling only at the batch rate. The chain would reach SimulateAll and exit
  # 3; better to be told at plan.
  validation {
    condition     = var.study == null || var.pipeline == null || var.study.model_provider != "bedrock"
    error_message = "The study chain cannot run on bedrock: it has no Message Batches API (ADR-0028). Use aws or anthropic with `pipeline`, or leave `pipeline` null and run the unbatched phases by hand."
  }
}

variable "observability" {
  description = <<-EOT
    Alerts and a dashboard for this sandbox, or null for none. All-or-nothing on
    purpose: the two thresholds depend on measurements nobody has made yet (how
    much memory the bench leaves free, how many connections it opens), so they
    have no default -- and a sandbox can be created before the platform root
    that owns the alerts topic exists.
  EOT
  type = object({
    alerts_topic_arn          = string
    freeable_memory_low_bytes = number
    database_connections_high = number
  })
  default = null
}

variable "experiment_clone" {
  description = "Create the copy-on-write clone for the partitioning experiment."
  type        = bool
  default     = false
}
