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
