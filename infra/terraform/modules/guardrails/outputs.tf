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
