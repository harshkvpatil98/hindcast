# The sandbox composition, offline.
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
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock" }
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
  mock_resource "aws_sfn_state_machine" {
    defaults = { arn = "arn:aws:states:us-east-1:123456789012:stateMachine:mock" }
  }
  mock_resource "aws_cloudwatch_event_rule" {
    defaults = { arn = "arn:aws:events:us-east-1:123456789012:rule/mock" }
  }
  mock_resource "aws_cloudwatch_metric_alarm" {
    defaults = { arn = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:mock" }
  }
  mock_resource "aws_efs_file_system" {
    defaults = { arn = "arn:aws:elasticfilesystem:us-east-1:123456789012:file-system/fs-mock" }
  }
  mock_resource "aws_efs_access_point" {
    defaults = { arn = "arn:aws:elasticfilesystem:us-east-1:123456789012:access-point/fsap-mock" }
  }
  mock_resource "aws_datasync_location_efs" {
    defaults = { arn = "arn:aws:datasync:us-east-1:123456789012:location/loc-efs" }
  }
  mock_resource "aws_datasync_location_s3" {
    defaults = { arn = "arn:aws:datasync:us-east-1:123456789012:location/loc-s3" }
  }
  mock_resource "aws_datasync_task" {
    defaults = { arn = "arn:aws:datasync:us-east-1:123456789012:task/task-mock" }
  }
  mock_resource "aws_eip" {
    defaults = { public_ip = "203.0.113.10" }
  }
}

mock_provider "random" {}

variables {
  region             = "us-east-1"
  availability_zones = ["us-east-1a", "us-east-1b"]

  # Shapes for the ADR-0042 runs below, which are not root variables: a
  # "study" on Claude Platform on AWS with no endpoint service named, so its
  # model calls must leave through the egress tier; the recovery object the
  # platform root outputs; the egress tier; the chains.
  recovery = {
    bucket_arn       = "arn:aws:s3:::cascade-recovery-123456789012-us-east-1"
    kms_key_arn      = "arn:aws:kms:us-east-1:123456789012:key/platform"
    llm_cache_prefix = "llm-cache"
  }
  reports = {
    bucket_arn  = "arn:aws:s3:::cascade-reports-123456789012-us-east-1"
    kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/platform"
    prefix      = "reports"
  }
  egress_tier = {
    availability_zone   = "us-east-1a"
    allowed_domains     = ["data.commoncrawl.org", "en.wikipedia.org", "www.federalregister.gov", "www.sec.gov", "api.gdeltproject.org"]
    dns_firewall_action = "BLOCK"
  }
  chains = {
    alerts_topic_arn       = "arn:aws:sns:us-east-1:123456789012:cascade-cost-alerts"
    fanout_timeout_seconds = 86400
  }
}

run "the_stack_composes_and_runs_tasks_without_public_ips" {
  command = apply

  assert {
    condition     = strcontains(output.run_task, "assignPublicIp=DISABLED")
    error_message = "Bench tasks must never get a public IP."
  }
  assert {
    condition     = strcontains(output.run_task, "--launch-type FARGATE")
    error_message = "The printed command must launch on Fargate."
  }
}

run "a_malformed_region_is_refused" {
  command = plan
  variables {
    region = "not-a-region"
  }
  expect_failures = [var.region]
}

run "observability_is_off_until_a_topic_and_thresholds_are_given" {
  command = plan

  assert {
    condition     = length(module.observability) == 0
    error_message = "With no observability input the sandbox must create no alarms -- and need no thresholds."
  }
}

run "observability_watches_this_sandbox_with_the_thresholds_given" {
  command = plan
  variables {
    observability = {
      alerts_topic_arn          = "arn:aws:sns:us-east-1:123456789012:cascade-cost-alerts"
      freeable_memory_low_bytes = 123456789
      database_connections_high = 37
    }
  }

  assert {
    condition     = length(module.observability) == 1
    error_message = "Given a topic and thresholds, the sandbox is watched."
  }
}

run "the_chains_exist_only_when_asked_for_and_feed_the_failure_alarms" {
  command = plan
  variables {
    pipeline = {
      alerts_topic_arn       = "arn:aws:sns:us-east-1:123456789012:cascade-cost-alerts"
      fanout_timeout_seconds = 86400
    }
    observability = {
      alerts_topic_arn          = "arn:aws:sns:us-east-1:123456789012:cascade-cost-alerts"
      freeable_memory_low_bytes = 123456789
      database_connections_high = 37
    }
  }

  assert {
    condition     = length(module.pipeline) == 1
    error_message = "Given a topic and a fan-out timeout, the two chains are created."
  }
}

run "no_chains_by_default" {
  command = plan

  assert {
    condition     = length(module.pipeline) == 0
    error_message = "A plain sandbox creates no state machines."
  }
}

# --- ADR-0042: the way out, the study task and the durable cache ---------------------------

run "the_default_sandbox_has_no_way_out_no_study_task_and_no_cache" {
  command = plan

  assert {
    condition     = length(module.egress) == 0 && output.egress == null
    error_message = "With no `egress` input the VPC is ADR-0034's: no internet gateway, no NAT, no subnet with a default route."
  }
  assert {
    condition     = length(module.study) == 0 && length(module.cache) == 0 && output.study == null
    error_message = "With no `study` input there is no task that can call a model and no file system."
  }
  assert {
    condition     = toset(keys(module.network.interface_endpoint_ids)) == toset(["ecr.api", "ecr.dkr", "logs", "secretsmanager"])
    error_message = "And the interface endpoints are the four the bench needs, no model endpoint among them."
  }
}

run "with_the_egress_tier_the_isolated_tier_still_has_no_route_out" {
  command = apply
  variables {
    egress   = var.egress_tier
    pipeline = var.chains
  }

  assert {
    condition     = !contains(module.egress[0].controls.default_route_table_ids, module.network.private_route_table_id)
    error_message = "The isolated tier's route table must never carry a default route."
  }
  assert {
    condition     = length(setintersection(toset(module.egress[0].controls.internet_routed_subnet_ids), toset(module.network.private_subnet_ids))) == 0
    error_message = "No isolated subnet may be associated with a table that routes to the internet."
  }
  assert {
    condition     = length(setintersection(toset(module.egress[0].controls.internet_routed_subnet_ids), toset(module.database.subnet_ids))) == 0
    error_message = "The database's subnet group must contain no subnet with a way out: egress is an exfiltration path (T13)."
  }
  assert {
    condition     = length(module.egress[0].controls.internet_routed_subnet_ids) == 2
    error_message = "Exactly the tier's own two subnets route out."
  }
  assert {
    condition     = tolist(module.egress[0].controls.availability_zones) == tolist(["us-east-1a"])
    error_message = "The tier lives in the AZ that was named."
  }
}

run "only_the_state_that_fetches_leaves_and_it_keeps_its_own_group" {
  command = apply
  variables {
    egress   = var.egress_tier
    pipeline = var.chains
  }

  assert {
    condition     = module.pipeline[0].controls.ingest.subnets.CorpusBuild == module.egress[0].subnet_ids
    error_message = "CorpusBuild -- the one ingest state that fetches -- runs in the egress tier's subnet."
  }
  assert {
    condition = alltrue([
      for state in ["RetrievalIndex", "RetrievalVerify", "CorpusVerify", "CorpusCoverage"] :
      module.pipeline[0].controls.ingest.subnets[state] == module.network.private_subnet_ids
    ])
    error_message = "The four ingest states that never fetch stay in the isolated subnets, with the admin credential they carry."
  }
  assert {
    condition     = toset(module.pipeline[0].controls.ingest.security_groups.CorpusBuild) == toset([module.bench.security_group_id, module.egress[0].security_group_id])
    error_message = "The egress group is added to the bench task's own, which still carries the database and endpoint rules."
  }
  assert {
    condition = alltrue([
      for state in ["RetrievalIndex", "RetrievalVerify", "CorpusVerify", "CorpusCoverage"] :
      !contains(module.pipeline[0].controls.ingest.security_groups[state], module.egress[0].security_group_id)
    ])
    error_message = "No other ingest state carries the egress group."
  }
  # No study here, so the study chain runs on the bench task and stays inside.
  assert {
    condition     = alltrue([for state, subnets in module.pipeline[0].controls.study.subnets : subnets == module.network.private_subnet_ids])
    error_message = "Without a study task there is no model path to move a state for."
  }
  assert {
    condition     = tolist(module.pipeline[0].controls.ingest.public_ip_settings) == tolist(["DISABLED"]) && tolist(module.pipeline[0].controls.study.public_ip_settings) == tolist(["DISABLED"])
    error_message = "Even in the egress tier a task never has a public address; the NAT is the way out."
  }
}

run "an_egress_az_outside_the_isolated_tier_is_refused" {
  command = plan
  variables {
    egress = merge(var.egress_tier, { availability_zone = "us-east-1c" })
  }
  expect_failures = [var.egress]
}

run "a_study_on_claude_platform_without_a_private_path_leaves_only_for_its_model_calls" {
  command = apply
  variables {
    egress   = var.egress_tier
    pipeline = var.chains
    study = {
      model_provider           = "aws"
      model_region             = "us-east-1"
      workspace_id             = "wrkspc_01ABC"
      recovery                 = var.recovery
      reports                  = var.reports
      sync_schedule_expression = "rate(1 hour)"
    }
  }

  assert {
    condition     = contains(module.egress[0].controls.allowed_domains, "aws-external-anthropic.us-east-1.api.aws")
    error_message = "The provider's host, from providers.py's template, joins the allow-list."
  }
  assert {
    condition = alltrue([
      for state in ["SimulateEstimate", "SimulateAll", "EvalGrid"] :
      module.pipeline[0].controls.study.subnets[state] == module.egress[0].subnet_ids
      && contains(module.pipeline[0].controls.study.security_groups[state], module.egress[0].security_group_id)
      && contains(module.pipeline[0].controls.study.security_groups[state], module.study[0].security_group_id)
    ])
    error_message = "The three states that call a model run in the egress subnet with the egress group added to the study task's own."
  }
  assert {
    condition = alltrue([
      for state in ["EnsembleCollapse", "Report"] :
      module.pipeline[0].controls.study.subnets[state] == module.network.private_subnet_ids
      && !contains(module.pipeline[0].controls.study.security_groups[state], module.egress[0].security_group_id)
    ])
    error_message = "The collapse and the report call no model and stay inside."
  }
  assert {
    condition     = alltrue([for state, arn in module.pipeline[0].controls.study.task_definitions : arn == module.study[0].task_definition_arn])
    error_message = "Every study state runs on the study task definition."
  }
  assert {
    condition     = alltrue([for state, arn in module.pipeline[0].controls.ingest.task_definitions : arn == module.bench.task_definition_arn])
    error_message = "Every ingest state still runs on the bench's."
  }
  assert {
    condition     = contains(module.database.client_labels, "study") && contains(module.database.client_labels, "bench")
    error_message = "The database admits the study task's group as well as the bench's."
  }
  assert {
    condition     = module.cache[0].controls.archive_subdirectory == "/llm-cache/cascade-sandbox/" && startswith(output.study.llm_cache_archive, "s3://cascade-recovery-123456789012-us-east-1/llm-cache/")
    error_message = "The cache is archived under the platform's prefix, in a directory named for this sandbox."
  }
  assert {
    condition     = module.cache[0].controls.encrypted && module.cache[0].controls.kms_key_arn == aws_kms_key.platform.arn
    error_message = "The cache is encrypted with this root's CMK."
  }
}

run "a_study_on_claude_platform_with_its_endpoint_named_never_leaves" {
  command = apply
  variables {
    egress   = var.egress_tier
    pipeline = var.chains
    study = {
      model_provider              = "aws"
      model_region                = "us-east-1"
      workspace_id                = "wrkspc_01ABC"
      model_endpoint_service_name = "com.amazonaws.us-east-1.example-claude-platform"
      recovery                    = var.recovery
      reports                     = var.reports
      sync_schedule_expression    = "rate(1 hour)"
    }
  }

  assert {
    condition     = module.network.named_interface_endpoint_services == { "claude-platform" = "com.amazonaws.us-east-1.example-claude-platform" }
    error_message = "The operator's endpoint service name becomes an interface endpoint, and is never guessed."
  }
  assert {
    condition     = alltrue([for state, subnets in module.pipeline[0].controls.study.subnets : subnets == module.network.private_subnet_ids])
    error_message = "With a private path, no study state leaves the isolated subnets."
  }
  assert {
    condition     = !contains(module.egress[0].controls.allowed_domains, "aws-external-anthropic.us-east-1.api.aws")
    error_message = "And the provider's public host is not on the allow-list."
  }
}

run "a_study_on_anthropic_needs_the_egress_tier" {
  command = plan
  variables {
    study = {
      model_provider = "anthropic"
      api_key_secret = {
        arn         = "arn:aws:secretsmanager:us-east-1:123456789012:secret:anthropic-api-key"
        kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/secrets"
      }
      recovery                 = var.recovery
      reports                  = var.reports
      sync_schedule_expression = "rate(1 hour)"
    }
  }
  expect_failures = [var.study]
}

run "a_study_on_anthropic_with_the_egress_tier_allows_the_api_host" {
  command = apply
  variables {
    egress = var.egress_tier
    study = {
      model_provider = "anthropic"
      api_key_secret = {
        arn         = "arn:aws:secretsmanager:us-east-1:123456789012:secret:anthropic-api-key"
        kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/secrets"
      }
      recovery                 = var.recovery
      reports                  = var.reports
      sync_schedule_expression = "rate(1 hour)"
    }
  }

  assert {
    condition     = contains(module.egress[0].controls.allowed_domains, "api.anthropic.com")
    error_message = "api.anthropic.com is on the allow-list, from providers.py, not typed in."
  }
  assert {
    condition     = contains(module.study[0].controls.secret_names, "CASCADE_ANTHROPIC_API_KEY")
    error_message = "The key arrives as an ECS secret."
  }
}

run "a_study_on_bedrock_gets_its_endpoint_and_cannot_have_the_chain" {
  command = plan
  variables {
    study = {
      model_provider           = "bedrock"
      model_region             = "us-east-1"
      recovery                 = var.recovery
      reports                  = var.reports
      sync_schedule_expression = "rate(1 hour)"
    }
  }

  assert {
    condition     = contains(keys(module.network.interface_endpoint_ids), "bedrock-mantle")
    error_message = "Bedrock is reached through com.amazonaws.<region>.bedrock-mantle, inside the VPC."
  }
  assert {
    condition     = length(module.egress) == 0
    error_message = "And needs no egress tier."
  }
}

run "the_study_chain_on_bedrock_is_refused_at_plan" {
  command = plan
  variables {
    pipeline = var.chains
    study = {
      model_provider           = "bedrock"
      model_region             = "us-east-1"
      recovery                 = var.recovery
      reports                  = var.reports
      sync_schedule_expression = "rate(1 hour)"
    }
  }
  expect_failures = [var.study]
}

run "a_private_path_in_another_region_is_refused" {
  command = plan
  variables {
    study = {
      model_provider           = "bedrock"
      model_region             = "us-west-2"
      recovery                 = var.recovery
      reports                  = var.reports
      sync_schedule_expression = "rate(1 hour)"
    }
  }
  expect_failures = [var.study]
}
