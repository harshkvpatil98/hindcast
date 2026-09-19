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
