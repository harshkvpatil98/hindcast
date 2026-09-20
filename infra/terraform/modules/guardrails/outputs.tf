output "regions_policy_json" {
  value = data.aws_iam_policy_document.regions.json
}

output "integrity_policy_json" {
  value = data.aws_iam_policy_document.integrity.json
}

output "policy_ids" {
  value = {
    regions   = aws_organizations_policy.regions.id
    integrity = aws_organizations_policy.integrity.id
  }
}

# The statement itself, not its rendered JSON: under mock providers the JSON is
# a placeholder, and a root-level test needs the real condition values.
output "regions_statement" {
  value = data.aws_iam_policy_document.regions.statement
}

# --- The Bedrock guardrail (ADR-0050) -------------------------------------------------------------

# The two values `providers.bedrock.guardrail_id` and
# `providers.bedrock.guardrail_version` take, in one object so the two roots
# cannot spell one and forget the other -- the same reason `modules/reports`
# publishes `publish` rather than three loose strings.
#
# `null` when no guardrail was asked for. Deliberately not an empty string: the
# audit distinguishes "no guardrail configured" from "a guardrail that found
# nothing", and an empty id would collapse that distinction at the one boundary
# where it is carried across a process.
output "guardrail" {
  description = "Feed `id` to providers.bedrock.guardrail_id and `version` to .guardrail_version. Null when none is configured."
  value = var.model_guardrail == null ? null : {
    id  = aws_bedrock_guardrail.model[0].guardrail_id
    arn = aws_bedrock_guardrail.model[0].guardrail_arn
    # The published number when one was published, else the resource's own
    # `version`, which is DRAFT. Reported either way rather than hidden, so a
    # reader of an audit can see it was measured against a mutable object.
    version = (
      var.publish_guardrail_version
      ? aws_bedrock_guardrail_version.model[0].version
      : aws_bedrock_guardrail.model[0].version
    )
    published = var.publish_guardrail_version
  }
}

# Read back from the resource, not echoed from the input: a test that asserts
# on this fails when the resource stops carrying what the caller asked for.
# Serialised, because the provider's nested-block representation is its own
# business and an assertion that depended on whether a block is a list or an
# object would be testing Terraform rather than this module.
output "guardrail_policy" {
  description = "What the created guardrail actually carries. For tests and review; not consumed by the study."
  value = var.model_guardrail == null ? null : {
    name              = aws_bedrock_guardrail.model[0].name
    blocked_input     = aws_bedrock_guardrail.model[0].blocked_input_messaging
    blocked_output    = aws_bedrock_guardrail.model[0].blocked_outputs_messaging
    content_policy    = jsonencode(aws_bedrock_guardrail.model[0].content_policy_config)
    topic_policy      = jsonencode(aws_bedrock_guardrail.model[0].topic_policy_config)
    published_version = length(aws_bedrock_guardrail_version.model)
  }
}
