output "vpc_id" {
  value = aws_vpc.this.id
}

output "vpc_cidr_block" {
  value = aws_vpc.this.cidr_block
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "s3_prefix_list_id" {
  description = "For security-group egress to S3 through the gateway endpoint."
  value       = aws_vpc_endpoint.s3.prefix_list_id
}

output "endpoint_security_group_id" {
  value = aws_security_group.endpoints.id
}

# For modules/egress and the tests that keep it away from these subnets: the
# opt-in egress tier associates its OWN route table with the S3 gateway
# endpoint, and a test asserts this table never appears among the tables that
# carry a default route.
output "private_route_table_id" {
  description = "The isolated tier's one route table. It has no default route, and nothing may give it one."
  value       = aws_route_table.private.id
}

output "s3_endpoint_id" {
  description = "So another tier's route table can reach S3 through the same gateway endpoint, and the same bucket allow-list."
  value       = aws_vpc_endpoint.s3.id
}

output "interface_endpoint_ids" {
  description = "By service key, so a caller can see which services have a private path."
  value       = { for key, endpoint in aws_vpc_endpoint.interface : key => endpoint.id }
}

output "named_interface_endpoint_services" {
  value = { for key, endpoint in aws_vpc_endpoint.named : key => endpoint.service_name }
}
