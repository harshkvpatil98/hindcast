output "study_ceiling_usd" {
  description = "Sum of the phase ceilings, as read from the study configuration."
  value       = local.study_ceiling
}

output "phase_ceilings_usd" {
  value = local.phase_ceilings
}

output "monthly_limit_usd" {
  value = local.monthly_limit
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

# Read from the policy document, so a root that forgot to pass a statement
# set shows up as a smaller count.
output "topic_policy_source_document_count" {
  value = length(data.aws_iam_policy_document.alerts.source_policy_documents)
}
