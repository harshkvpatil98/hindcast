# The egress tier: the one deliberate way out of an otherwise isolated VPC
# (ADR-0042). It exists because the corpus ingest fetches from the public
# internet -- CC-NEWS over HTTPS from data.commoncrawl.org, the MediaWiki API,
# the Federal Register, EDGAR, GDELT -- and modules/network, correctly, gives
# nothing a path there (ADR-0034).
#
# What it is: one public subnet holding one NAT gateway, and one private
# "workload" subnet whose route table is the ONLY one in the VPC with a default
# route. A task reaches the internet by being launched in the workload subnet
# with this module's security group added to its own; nothing else changes.
# The database, the bench, the interface endpoints and the cache's mount
# targets stay in modules/network's subnets, whose route table this module
# never sees. An internet gateway attached to a VPC gives no subnet a path by
# itself -- a route does -- and the routes are all here.
#
# What it is not: a filter. A NAT gateway forwards to any address, and a
# security group cannot name a domain. The narrowing is DNS Firewall, below: an
# allow-list of the names the ingest actually resolves, and NXDOMAIN for
# everything else. That stops a task that looks a name up. It does NOT stop one
# that connects to an address it already holds; see "Residual risk" in
# ADR-0042 and T13 in the threat model.
#
# Why not AWS Network Firewall, which does filter: measured against this
# study's budget (a few hundred dollars in total) a firewall endpoint is billed
# per hour whether or not an ingest is running, and the cost of forgetting to
# destroy it for a month is on the order of the whole study. The cost of
# forgetting a NAT gateway is an order of magnitude less. ADR-0042 has the
# arithmetic and the prices that could and could not be verified.
#
# One AZ, on purpose. The ingest is one serial, resumable task (modules/
# pipeline); a second NAT would double the standing cost to protect a batch job
# from an AZ outage it survives by being re-run. And a task in one AZ sending
# through a NAT in another pays cross-AZ transfer on every byte of the crawl.

data "aws_region" "current" {}

locals {
  region = data.aws_region.current.region

  # Names every task in this VPC resolves whether or not it ever leaves it:
  # the regional AWS endpoints behind the interface and gateway endpoints, RDS,
  # ECR, EFS. DNS Firewall is associated with the VPC, not with a subnet, so a
  # block-everything rule without these would take the isolated tier down with
  # it. Regional on purpose: "*.amazonaws.com" would also resolve S3 in every
  # OTHER region, which the gateway endpoint's bucket allow-list does not
  # cover -- an exfiltration path by way of somebody else's bucket.
  #
  # Derived from the endpoints this design uses, and NOT verified live. The
  # likeliest omission is a client that still resolves a global name
  # (s3.amazonaws.com) where a regional one exists -- which is what running in
  # ALERT first, and reading the query log, is for.
  aws_names = [
    "*.${local.region}.amazonaws.com",
    "*.${local.region}.api.aws",
  ]
}

# --- The way out -----------------------------------------------------------------

resource "aws_internet_gateway" "this" {
  vpc_id = var.vpc_id
  tags   = { Name = "${var.name}-egress" }
}

resource "aws_subnet" "public" {
  vpc_id            = var.vpc_id
  availability_zone = var.availability_zone
  cidr_block        = var.public_subnet_cidr
  # Holds the NAT gateway and nothing else. No task is ever launched here, and
  # nothing launched here by mistake would be given an address.
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.name}-egress-public", Tier = "egress-public" }
}

resource "aws_route_table" "public" {
  vpc_id = var.vpc_id
  tags   = { Name = "${var.name}-egress-public" }
}

resource "aws_route" "public_default" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.this.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.name}-egress-nat" }
  # AWS's own ordering rule: an address cannot be allocated for a VPC that has
  # no internet gateway yet.
  depends_on = [aws_internet_gateway.this]
}

resource "aws_nat_gateway" "this" {
  allocation_id     = aws_eip.nat.id
  subnet_id         = aws_subnet.public.id
  connectivity_type = "public"
  tags              = { Name = "${var.name}-egress" }
  depends_on        = [aws_internet_gateway.this]
}

# --- Where an egress task runs --------------------------------------------------------

resource "aws_subnet" "workload" {
  vpc_id                  = var.vpc_id
  availability_zone       = var.availability_zone
  cidr_block              = var.workload_subnet_cidr
  map_public_ip_on_launch = false
  tags                    = { Name = "${var.name}-egress-workload", Tier = "egress-workload" }
}

resource "aws_route_table" "workload" {
  vpc_id = var.vpc_id
  tags   = { Name = "${var.name}-egress-workload" }
}

resource "aws_route" "workload_default" {
  route_table_id         = aws_route_table.workload.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this.id
}

resource "aws_route_table_association" "workload" {
  subnet_id      = aws_subnet.workload.id
  route_table_id = aws_route_table.workload.id
}

# S3 in this region still goes through the gateway endpoint: image layers and
# the artifacts bucket are not billed NAT processing, and -- the reason that
# matters -- stay under the endpoint policy's bucket allow-list rather than
# taking the open road.
resource "aws_vpc_endpoint_route_table_association" "workload_s3" {
  route_table_id  = aws_route_table.workload.id
  vpc_endpoint_id = var.s3_gateway_endpoint_id
}

# --- The security group an egress task adds to its own -------------------------------------

resource "aws_security_group" "egress" {
  #checkov:skip=CKV2_AWS_5:Attached at run time -- Step Functions passes it to ecs:RunTask for the states that fetch (modules/pipeline), and to no other state.
  name        = "${var.name}-egress"
  description = "Added to a task that must fetch from the internet: HTTPS out, nothing in"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-egress" }
}

# The one open CIDR in the repository. A security group cannot name a domain,
# and the sources sit behind CDNs whose addresses change; 443 and nothing else
# is as narrow as this layer goes. No port 80: every source is fetched over
# HTTPS (cascade/corpus/sources/*), and plaintext would be a path with no
# server identity at all.
resource "aws_vpc_security_group_egress_rule" "https_out" {
  security_group_id = aws_security_group.egress.id
  description       = "HTTPS to the internet through the NAT gateway; names are narrowed by DNS Firewall"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# --- DNS Firewall: an allow-list, then nothing -----------------------------------------------

resource "aws_route53_resolver_firewall_domain_list" "allowed" {
  name    = "${var.name}-egress-allowed"
  domains = sort(distinct(concat(var.allowed_domains, local.aws_names)))
}

resource "aws_route53_resolver_firewall_domain_list" "everything" {
  name = "${var.name}-egress-everything"
  # A specification "can optionally start with *" (Route 53 developer guide);
  # a lone asterisk as match-everything is NOT verified live.
  domains = ["*"]
}

resource "aws_route53_resolver_firewall_rule_group" "this" {
  name = "${var.name}-egress"
}

resource "aws_route53_resolver_firewall_rule" "allow" {
  name                    = "${var.name}-allow-listed"
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.this.id
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.allowed.id
  priority                = 100
  action                  = "ALLOW"
  # The sources are served from CDNs: data.commoncrawl.org is a CNAME into
  # CloudFront. Inspecting the chain would require allow-listing CDN hostnames
  # that change without notice; trusting it means an allowed domain's owner
  # decides where it points, which they do anyway.
  firewall_domain_redirection_action = "TRUST_REDIRECTION_DOMAIN"
}

resource "aws_route53_resolver_firewall_rule" "everything_else" {
  name                    = "${var.name}-everything-else"
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.this.id
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.everything.id
  priority                = 200
  # ALERT or BLOCK, and the caller must say which (var.dns_firewall_action).
  action = var.dns_firewall_action
  # NXDOMAIN rather than NODATA or an override: the fetcher sees "no such
  # host", which its own retry logic already treats as a failure to report.
  block_response = var.dns_firewall_action == "BLOCK" ? "NXDOMAIN" : null
}

resource "aws_route53_resolver_firewall_rule_group_association" "this" {
  name                   = "${var.name}-egress"
  firewall_rule_group_id = aws_route53_resolver_firewall_rule_group.this.id
  vpc_id                 = var.vpc_id
  # 101 is the lowest priority a customer association may take; nothing else
  # is associated with this VPC, so nothing is evaluated before it.
  priority = 101
}

# If DNS Firewall itself cannot answer, queries FAIL. The alternative lets
# every name through exactly when the control is down, and the workload is a
# batch job that resumes: availability is the cheaper thing to give up.
resource "aws_route53_resolver_firewall_config" "this" {
  resource_id        = var.vpc_id
  firewall_fail_open = "DISABLED"
}

# --- The record of what was asked for ------------------------------------------------------------

# Every query from the VPC, with the firewall's verdict. This is how the
# allow-list is validated before it is enforced (run in ALERT, read this, then
# BLOCK -- AWS's own recommendation), and afterwards the only place a blocked
# lookup from a compromised task would show.
resource "aws_cloudwatch_log_group" "dns" {
  #checkov:skip=CKV_AWS_338:Sandbox retention, deliberately short: the tier is created for an ingest and destroyed after it. The platform's audit trail keeps 365 days.
  name              = "/cascade/${var.name}/dns"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.log_kms_key_arn
}

resource "aws_route53_resolver_query_log_config" "this" {
  name            = "${var.name}-egress"
  destination_arn = aws_cloudwatch_log_group.dns.arn
}

resource "aws_route53_resolver_query_log_config_association" "this" {
  resolver_query_log_config_id = aws_route53_resolver_query_log_config.this.id
  resource_id                  = var.vpc_id
}
