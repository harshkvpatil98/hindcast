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

# --- The Bedrock guardrail (ADR-0050) -------------------------------------------------------------
#
# A different noun from the SCPs above, at a different layer, answering the same
# question: what this account may not do. The SCPs bound the AWS API; this
# bounds what a model may emit. It is created here and applied nowhere --
# `cascade eval guardrails` asks it what it *would* have done to the compiled
# graphs, and ADR-0050 admits it into the compile path only if the answer is
# that it would have done nothing.

variable "model_guardrail" {
  description = <<-EOT
    The Bedrock guardrail to create, or null to create none.

    Null by default, and the default is the whole design. A guardrail that
    alters one of the 180 decompositions changes the experiment (ADR-0030), so
    it must exist before it can be measured and must not be reachable by the
    compile path until it has been. Creating it is therefore separable from
    using it, exactly as `target_ids = []` makes the SCPs separable from
    binding them.

    `blocked_input_messaging` and `blocked_outputs_messaging` are required by
    the service and are never seen by this project: the audit reads `action`,
    not the substitute text. They are named anyway rather than defaulted,
    because a deployment that later puts this guardrail in a call path would
    surface them to a model and a placeholder chosen here would be the string
    it returned.

    `content_filters` names one `{type, input_strength, output_strength}` per
    filter, in the service's own vocabulary (`HATE`, `VIOLENCE`,
    `PROMPT_ATTACK`, ...), unvalidated here on purpose: the valid set is the
    provider's and pinning a copy of it in this module would reject a filter
    AWS added without telling us why.
  EOT

  type = object({
    name                      = string
    description               = optional(string)
    blocked_input_messaging   = string
    blocked_outputs_messaging = string
    content_filters = optional(list(object({
      type            = string
      input_strength  = string
      output_strength = string
    })), [])
    denied_topics = optional(list(object({
      name       = string
      definition = string
      examples   = optional(list(string), [])
    })), [])
  })
  default = null

  # A guardrail with no policy is a guardrail that cannot intervene, and an
  # audit of one would report "clear" over an empty configuration -- a zero
  # that means "there was nothing to check", dressed as a measurement. That is
  # the exact failure ADR-0050 exists to keep apart from a real clear result,
  # so it is refused here rather than reported later.
  validation {
    condition = (
      var.model_guardrail == null ||
      length(coalesce(try(var.model_guardrail.content_filters, []), [])) > 0 ||
      length(coalesce(try(var.model_guardrail.denied_topics, []), [])) > 0
    )
    error_message = "model_guardrail must configure at least one content filter or denied topic: a guardrail with no policy cannot intervene, and auditing one would report a clear result over nothing."
  }
}

variable "publish_guardrail_version" {
  description = <<-EOT
    Publish an immutable numbered version of the guardrail, and report that
    number instead of DRAFT.

    False by default. DRAFT is mutable, so an audit of DRAFT measures whatever
    the guardrail happened to be at the time and cannot be re-checked against
    the same object later. Publish before any result is quoted; leave it false
    while the policies are still being written.
  EOT
  type        = bool
  default     = false
}
