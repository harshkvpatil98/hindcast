output "plan_role_arn" {
  value = aws_iam_role.plan.arn
}

output "apply_role_arn" {
  value = aws_iam_role.apply.arn
}

# Read from the policy documents, not echoed from the inputs.
output "controls" {
  value = {
    plan_subjects  = flatten([for s in data.aws_iam_policy_document.plan_trust.statement : [for c in s.condition : c.values if endswith(c.variable, ":sub")]])
    apply_subjects = flatten([for s in data.aws_iam_policy_document.apply_trust.statement : [for c in s.condition : c.values if endswith(c.variable, ":sub")]])
    subject_tests  = distinct(flatten([for d in [data.aws_iam_policy_document.plan_trust, data.aws_iam_policy_document.apply_trust] : [for s in d.statement : [for c in s.condition : c.test if endswith(c.variable, ":sub")]]]))
    audiences      = distinct(flatten([for d in [data.aws_iam_policy_document.plan_trust, data.aws_iam_policy_document.apply_trust] : [for s in d.statement : [for c in s.condition : c.values if endswith(c.variable, ":aud")]]]))
    plan_policies  = [aws_iam_role_policy_attachment.plan_read_only.policy_arn]
    apply_policies = sort([for a in aws_iam_role_policy_attachment.apply : a.policy_arn])
  }
}
