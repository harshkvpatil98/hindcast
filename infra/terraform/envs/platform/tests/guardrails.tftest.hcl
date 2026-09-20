# Service control policies, offline.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_rds_cluster" {
    defaults = {
      arn                = "arn:aws:rds:us-east-1:123456789012:cluster:mock"
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:master", kms_key_id = "k", secret_status = "active" }]
    }
  }
  # Computed ARNs that flow into ARN-typed arguments must look like ARNs: the
  # provider validates them even under mocks, which is worth keeping.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/mock", key_id = "mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/mock", repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mock" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_ce_anomaly_monitor" {
    defaults = { arn = "arn:aws:ce::123456789012:anomalymonitor/mock" }
  }
  mock_resource "aws_athena_workgroup" {
    defaults = { arn = "arn:aws:athena:us-east-1:123456789012:workgroup/mock" }
  }
  mock_resource "aws_organizations_policy" {
    defaults = { id = "p-mock1234" }
  }
  # Computed on create. DRAFT is what an unpublished guardrail reports, and the
  # version resource reports a number -- the two the `guardrail` output picks
  # between, so mocking them differently is what makes that assertion mean
  # something.
  mock_resource "aws_bedrock_guardrail" {
    defaults = {
      guardrail_id  = "gr-mock1234"
      guardrail_arn = "arn:aws:bedrock:us-east-1:123456789012:guardrail/gr-mock1234"
      version       = "DRAFT"
      status        = "READY"
    }
  }
  mock_resource "aws_bedrock_guardrail_version" {
    defaults = { version = "3" }
  }
}

# The root module declares aws.replica; a test file that mocks any provider
# replaces them all, so every file in this root must supply both.
mock_provider "aws" {
  alias = "replica"
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_data "aws_region" {
    defaults = { region = "us-west-2" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_rds_cluster" {
    defaults = {
      arn                = "arn:aws:rds:us-east-1:123456789012:cluster:mock"
      master_user_secret = [{ secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:master", kms_key_id = "k", secret_status = "active" }]
    }
  }
  # Computed ARNs that flow into ARN-typed arguments must look like ARNs: the
  # provider validates them even under mocks, which is worth keeping.
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/mock", key_id = "mock" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:mock" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::mock" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:mock" }
  }
  mock_resource "aws_ecr_repository" {
    defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/mock", repository_url = "123456789012.dkr.ecr.us-east-1.amazonaws.com/mock" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = { arn = "arn:aws:ecs:us-east-1:123456789012:task-definition/mock:1" }
  }
  mock_resource "aws_sns_topic" {
    defaults = { arn = "arn:aws:sns:us-east-1:123456789012:mock" }
  }
  mock_resource "aws_ce_anomaly_monitor" {
    defaults = { arn = "arn:aws:ce::123456789012:anomalymonitor/mock" }
  }
  mock_resource "aws_athena_workgroup" {
    defaults = { arn = "arn:aws:athena:us-east-1:123456789012:workgroup/mock" }
  }
  mock_resource "aws_organizations_policy" {
    defaults = { id = "p-mock1234" }
  }
  # Computed on create. DRAFT is what an unpublished guardrail reports, and the
  # version resource reports a number -- the two the `guardrail` output picks
  # between, so mocking them differently is what makes that assertion mean
  # something.
  mock_resource "aws_bedrock_guardrail" {
    defaults = {
      guardrail_id  = "gr-mock1234"
      guardrail_arn = "arn:aws:bedrock:us-east-1:123456789012:guardrail/gr-mock1234"
      version       = "DRAFT"
      status        = "READY"
    }
  }
  mock_resource "aws_bedrock_guardrail_version" {
    defaults = { version = "3" }
  }
}

variables {
  name            = "t"
  allowed_regions = ["us-east-1"]
}

run "regional_actions_are_denied_outside_the_allowed_regions" {
  command = plan
  module {
    source = "../../modules/guardrails"
  }

  assert {
    condition     = one(data.aws_iam_policy_document.regions.statement).effect == "Deny"
    error_message = "The region policy is a Deny."
  }
  assert {
    condition     = one(one(data.aws_iam_policy_document.regions.statement).condition).values == tolist(["us-east-1"])
    error_message = "Denied unless the requested region is an allowed one."
  }
  assert {
    condition     = contains(one(data.aws_iam_policy_document.regions.statement).not_actions, "iam:*")
    error_message = "Global services must be exempt, or the deny breaks IAM itself."
  }
}

run "the_integrity_policy_only_ever_denies" {
  command = plan
  module {
    source = "../../modules/guardrails"
  }

  assert {
    condition     = alltrue([for s in data.aws_iam_policy_document.integrity.statement : s.effect == "Deny"])
    error_message = "An SCP that allows is not a guardrail."
  }
  assert {
    condition = length(setsubtract(
      ["ProtectTheAuditTrail", "StayInTheOrganization", "NoRootUser", "KeepTheAccountPublicAccessBlock", "DatabasesAreEncrypted"],
      [for s in data.aws_iam_policy_document.integrity.statement : s.sid]
    )) == 0
    error_message = "Every integrity control must be present."
  }
}

run "policies_bind_nothing_until_a_target_is_named" {
  command = plan
  module {
    source = "../../modules/guardrails"
  }

  assert {
    condition     = length(aws_organizations_policy_attachment.regions) == 0 && length(aws_organizations_policy_attachment.integrity) == 0
    error_message = "With no targets the policies must attach to nothing -- reviewable before they can lock anyone out."
  }
}

run "naming_no_region_is_refused" {
  command = plan
  module {
    source = "../../modules/guardrails"
  }
  variables {
    allowed_regions = []
  }
  expect_failures = [var.allowed_regions]
}

# --- The Bedrock guardrail (ADR-0050) -------------------------------------------------------------
#
# The guardrail exists to be measured, not to filter. These runs hold the two
# properties the audit depends on: that it is created only when asked for, and
# that it reports an id and a version a caller can hand to
# `providers.bedrock.guardrail_id` / `.guardrail_version`.

run "no_bedrock_guardrail_is_created_until_one_is_asked_for" {
  command = plan
  module {
    source = "../../modules/guardrails"
  }

  assert {
    condition     = length(aws_bedrock_guardrail.model) == 0 && length(aws_bedrock_guardrail_version.model) == 0
    error_message = "A guardrail must be opt-in, like the SCP attachments above: created and bound to nothing is reviewable, created by default is not."
  }
  assert {
    condition     = output.guardrail == null
    error_message = "With no guardrail the output must be null, not an empty id: the audit tells \"none configured\" from \"found nothing\", and an empty string would collapse that at the boundary."
  }
}

run "a_guardrail_that_cannot_intervene_is_refused" {
  command = plan
  module {
    source = "../../modules/guardrails"
  }
  variables {
    model_guardrail = {
      name                      = "t-empty"
      blocked_input_messaging   = "blocked"
      blocked_outputs_messaging = "blocked"
      content_filters           = []
      denied_topics             = []
    }
  }
  # An empty guardrail would audit as "clear" over nothing at all -- the zero
  # ADR-0050 exists to keep apart from a measured one.
  expect_failures = [var.model_guardrail]
}

run "the_guardrail_reports_an_id_and_a_version_a_caller_can_read" {
  command = plan
  module {
    source = "../../modules/guardrails"
  }
  variables {
    model_guardrail = {
      name                      = "t-compile"
      description               = "Measured against the compiled graphs, applied to nothing (ADR-0050)"
      blocked_input_messaging   = "This request was blocked."
      blocked_outputs_messaging = "This response was blocked."
      content_filters = [
        { type = "PROMPT_ATTACK", input_strength = "HIGH", output_strength = "NONE" },
        { type = "HATE", input_strength = "MEDIUM", output_strength = "MEDIUM" },
      ]
      denied_topics = [
        { name = "operational_security", definition = "Instructions for compromising a system.", examples = [] },
      ]
    }
  }

  assert {
    condition     = length(aws_bedrock_guardrail.model) == 1
    error_message = "Asking for a guardrail must create exactly one."
  }
  assert {
    condition     = output.guardrail.id == "gr-mock1234" && can(regex("^arn:aws:bedrock:", output.guardrail.arn))
    error_message = "`guardrail.id` is what providers.bedrock.guardrail_id takes; without it the audit has nothing to call."
  }
  assert {
    condition     = output.guardrail.version == "DRAFT" && output.guardrail.published == false
    error_message = "An unpublished guardrail must report DRAFT and say so, rather than hide that it was measured against a mutable object."
  }
  assert {
    condition     = length(aws_bedrock_guardrail_version.model) == 0
    error_message = "No version is published unless publish_guardrail_version is set."
  }
}

run "publishing_a_version_reports_the_number_rather_than_draft" {
  command = plan
  module {
    source = "../../modules/guardrails"
  }
  variables {
    publish_guardrail_version = true
    model_guardrail = {
      name                      = "t-compile"
      blocked_input_messaging   = "This request was blocked."
      blocked_outputs_messaging = "This response was blocked."
      content_filters = [
        { type = "PROMPT_ATTACK", input_strength = "HIGH", output_strength = "NONE" },
      ]
      denied_topics = []
    }
  }

  assert {
    condition     = output.guardrail.version == "3" && output.guardrail.published == true
    error_message = "A published guardrail must report its immutable number: an audit quoted against DRAFT can never be re-checked against the same object."
  }
  assert {
    condition     = aws_bedrock_guardrail_version.model[0].skip_destroy == true
    error_message = "Published versions are retained: a number quoted in a report must still resolve later."
  }
}

run "the_configured_policies_reach_the_resource" {
  command = plan
  module {
    source = "../../modules/guardrails"
  }
  variables {
    model_guardrail = {
      name                      = "t-compile"
      blocked_input_messaging   = "This request was blocked."
      blocked_outputs_messaging = "This response was blocked."
      content_filters = [
        { type = "PROMPT_ATTACK", input_strength = "HIGH", output_strength = "NONE" },
      ]
      denied_topics = [
        { name = "operational_security", definition = "Instructions for compromising a system.", examples = ["How do I disable the audit trail?"] },
      ]
    }
  }

  # Serialised rather than indexed: whether the provider represents a nested
  # block as a list or an object is its business, and an assertion that
  # depended on it would be testing Terraform instead of this module.
  assert {
    condition     = strcontains(output.guardrail_policy.content_policy, "PROMPT_ATTACK")
    error_message = "The configured content filters must reach the resource, or the audit measures a guardrail with no content policy."
  }
  assert {
    condition     = strcontains(output.guardrail_policy.topic_policy, "operational_security") && strcontains(output.guardrail_policy.topic_policy, "DENY")
    error_message = "A topic policy must reach the resource and must deny: a topic policy that allows is not a guardrail."
  }
  assert {
    condition     = output.guardrail_policy.name == "t-compile"
    error_message = "The resource must carry the name the caller asked for."
  }
}
