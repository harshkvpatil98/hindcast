# The egress tier, offline (ADR-0042): one way out, for the states that
# fetch, and the isolated tier's tables never among the ones that route there.
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
  name                   = "t"
  vpc_id                 = "vpc-0123"
  availability_zone      = "us-east-1a"
  public_subnet_cidr     = "10.40.255.0/24"
  workload_subnet_cidr   = "10.40.254.0/24"
  s3_gateway_endpoint_id = "vpce-s3"
  allowed_domains        = ["data.commoncrawl.org", "en.wikipedia.org", "www.federalregister.gov"]
  dns_firewall_action    = "BLOCK"
  log_kms_key_arn        = "arn:aws:kms:us-east-1:123456789012:key/platform"
}

run "one_az_two_subnets_and_no_public_addresses" {
  command = apply
  module {
    source = "../../modules/egress"
  }

  assert {
    condition     = tolist(output.controls.availability_zones) == tolist([var.availability_zone])
    error_message = "Both subnets live in the one AZ that was named: a task in another AZ would pay cross-AZ transfer for every byte of the crawl."
  }
  assert {
    condition     = length(output.controls.subnets_assigning_public_ips) == 0
    error_message = "No subnet in the tier assigns public addresses; the NAT gateway holds the only one."
  }
  assert {
    condition     = aws_nat_gateway.this.subnet_id == aws_subnet.public.id && aws_nat_gateway.this.connectivity_type == "public"
    error_message = "The NAT gateway sits in the public subnet and faces the internet."
  }
  assert {
    condition     = tolist(output.subnet_ids) == tolist([aws_subnet.workload.id])
    error_message = "Tasks are launched in the workload subnet and never in the public one."
  }
}

run "exactly_two_tables_route_out_and_neither_is_anybody_elses" {
  command = apply
  module {
    source = "../../modules/egress"
  }

  assert {
    condition     = aws_route.public_default.gateway_id == aws_internet_gateway.this.id && aws_route.public_default.destination_cidr_block == "0.0.0.0/0"
    error_message = "The public table's default route is the internet gateway."
  }
  assert {
    condition     = aws_route.workload_default.nat_gateway_id == aws_nat_gateway.this.id && aws_route.workload_default.destination_cidr_block == "0.0.0.0/0"
    error_message = "The workload table's default route is the NAT gateway -- never the internet gateway, which would need a public address."
  }
  assert {
    condition     = toset(output.controls.default_route_table_ids) == toset([aws_route_table.public.id, aws_route_table.workload.id])
    error_message = "Only the two tables this module creates carry a default route; it takes no other table as input."
  }
  assert {
    condition     = toset(output.controls.internet_routed_subnet_ids) == toset([aws_subnet.public.id, aws_subnet.workload.id])
    error_message = "Only the two subnets this module creates are associated with a way out."
  }
  assert {
    condition     = output.controls.s3_stays_on_gateway_endpoint
    error_message = "In-region S3 from the workload subnet goes through the gateway endpoint and its bucket allow-list, not through the NAT."
  }
}

run "the_security_group_opens_443_out_and_nothing_else" {
  command = apply
  module {
    source = "../../modules/egress"
  }

  assert {
    condition     = tolist(output.controls.open_egress_ports) == tolist(["tcp:443-443"])
    error_message = "HTTPS out is the whole of it: no port 80, no all-ports rule."
  }
  assert {
    condition     = aws_vpc_security_group_egress_rule.https_out.cidr_ipv4 == "0.0.0.0/0"
    error_message = "The sources sit behind CDNs; the narrowing is by name in DNS Firewall, not by address here."
  }
}

run "dns_firewall_allows_the_list_then_refuses_every_other_name" {
  command = apply
  module {
    source = "../../modules/egress"
  }

  assert {
    condition     = output.controls.rule_actions_by_priority == { "100" = "ALLOW", "200" = "BLOCK" }
    error_message = "The allow-list is evaluated first, and everything else is blocked."
  }
  assert {
    condition     = aws_route53_resolver_firewall_rule.everything_else.block_response == "NXDOMAIN"
    error_message = "A refused name does not exist, as far as the task can tell."
  }
  assert {
    condition     = aws_route53_resolver_firewall_domain_list.everything.domains == toset(["*"])
    error_message = "The block rule matches every name."
  }
  assert {
    condition     = length(setsubtract(toset(var.allowed_domains), toset(output.controls.allowed_domains))) == 0
    error_message = "Every domain the caller named is on the allow-list."
  }
  assert {
    condition     = contains(output.controls.allowed_domains, "*.us-east-1.amazonaws.com") && !contains(output.controls.allowed_domains, "*.amazonaws.com")
    error_message = "AWS names are allowed for THIS region only: a global wildcard would resolve S3 in every other region, past the gateway endpoint's bucket list."
  }
  assert {
    condition     = output.controls.dns_firewall_associated_vpc == var.vpc_id && output.controls.dns_queries_are_logged_for == var.vpc_id
    error_message = "The rule group and the query log are associated with the VPC."
  }
  assert {
    condition     = output.controls.dns_firewall_fails_open == "DISABLED"
    error_message = "If DNS Firewall cannot answer, names fail: availability is the cheaper thing to give up for a resumable batch job."
  }
  assert {
    condition     = aws_route53_resolver_firewall_rule.allow.firewall_domain_redirection_action == "TRUST_REDIRECTION_DOMAIN"
    error_message = "CNAME chains into CDNs are trusted from the allowed name, or every CDN hostname would need listing."
  }
}

run "alert_mode_logs_the_would_be_refusals_instead" {
  command = apply
  module {
    source = "../../modules/egress"
  }
  variables {
    dns_firewall_action = "ALERT"
  }

  assert {
    condition     = output.controls.rule_actions_by_priority["200"] == "ALERT" && aws_route53_resolver_firewall_rule.everything_else.block_response == null
    error_message = "ALERT answers the query and logs it -- how the list is proven complete before it is enforced."
  }
}

run "the_query_log_is_encrypted_under_the_cascade_prefix" {
  command = plan
  module {
    source = "../../modules/egress"
  }

  assert {
    condition     = aws_cloudwatch_log_group.dns.kms_key_id == var.log_kms_key_arn && startswith(aws_cloudwatch_log_group.dns.name, "/cascade/")
    error_message = "The DNS log is encrypted with the CMK, under the prefix the key policy admits."
  }
}

run "an_empty_allow_list_is_refused" {
  command = plan
  module {
    source = "../../modules/egress"
  }
  variables {
    allowed_domains = []
  }
  expect_failures = [var.allowed_domains]
}

run "a_bare_star_is_refused" {
  command = plan
  module {
    source = "../../modules/egress"
  }
  variables {
    allowed_domains = ["*"]
  }
  expect_failures = [var.allowed_domains]
}

run "a_wildcard_over_a_whole_tld_is_refused" {
  command = plan
  module {
    source = "../../modules/egress"
  }
  variables {
    allowed_domains = ["*.org"]
  }
  expect_failures = [var.allowed_domains]
}

run "an_unknown_firewall_action_is_refused" {
  command = plan
  module {
    source = "../../modules/egress"
  }
  variables {
    dns_firewall_action = "ALLOW"
  }
  expect_failures = [var.dns_firewall_action]
}
