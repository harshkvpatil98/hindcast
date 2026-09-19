output "subnet_ids" {
  description = "Where a task that must fetch is launched. One subnet, one AZ."
  value       = [aws_subnet.workload.id]
}

output "security_group_id" {
  description = "Added to -- never instead of -- the task's own group, which still carries its database, endpoint and cache rules."
  value       = aws_security_group.egress.id
}

output "nat_public_ip" {
  description = "The address every egress task is seen as. Sources that rate-limit by address (GDELT, EDGAR) see this one."
  value       = aws_eip.nat.public_ip
}

output "dns_log_group" {
  value = aws_cloudwatch_log_group.dns.name
}

# Read from the resources, not echoed from the inputs. The root's tests use
# these to prove a negative: that no subnet and no route table of the isolated
# tier appears among the ones with a way out.
output "controls" {
  value = {
    internet_routed_subnet_ids   = [for a in [aws_route_table_association.public, aws_route_table_association.workload] : a.subnet_id]
    default_route_table_ids      = [for r in [aws_route.public_default, aws_route.workload_default] : r.route_table_id]
    subnets_assigning_public_ips = [for s in [aws_subnet.public, aws_subnet.workload] : s.id if s.map_public_ip_on_launch]
    availability_zones           = distinct([aws_subnet.public.availability_zone, aws_subnet.workload.availability_zone])
    open_egress_ports            = [for r in [aws_vpc_security_group_egress_rule.https_out] : "${r.ip_protocol}:${r.from_port}-${r.to_port}"]
    allowed_domains              = aws_route53_resolver_firewall_domain_list.allowed.domains
    rule_actions_by_priority     = { for r in [aws_route53_resolver_firewall_rule.allow, aws_route53_resolver_firewall_rule.everything_else] : tostring(r.priority) => r.action }
    dns_firewall_fails_open      = aws_route53_resolver_firewall_config.this.firewall_fail_open
    dns_queries_are_logged_for   = aws_route53_resolver_query_log_config_association.this.resource_id
    s3_stays_on_gateway_endpoint = aws_vpc_endpoint_route_table_association.workload_s3.route_table_id == aws_route_table.workload.id
    dns_firewall_associated_vpc  = aws_route53_resolver_firewall_rule_group_association.this.vpc_id
  }
}
