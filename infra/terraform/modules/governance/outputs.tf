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
