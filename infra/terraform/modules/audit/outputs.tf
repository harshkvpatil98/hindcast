output "trail_arn" {
  value = aws_cloudtrail.this.arn
}

output "log_bucket" {
  value = aws_s3_bucket.trail.bucket
}

output "config_bucket" {
  value = aws_s3_bucket.config.bucket
}

output "detector_id" {
  value = aws_guardduty_detector.this.id
}

output "required_key_policy_statements_json" {
  description = <<-EOT
    Statements the caller's KMS key policy must carry; merge with
    `source_policy_documents`. Depends on the name, account and region only,
    never on the key, so using it in the key's own policy is not a cycle.
  EOT
  value       = data.aws_iam_policy_document.required_key_policy.json
}

# Read from the resources, not echoed from the inputs, so a test that asserts
# on this output fails when a resource changes -- including from the root,
# where the module's resources are out of a test's reach.
output "controls" {
  value = {
    multi_region             = aws_cloudtrail.this.is_multi_region_trail
    global_service_events    = aws_cloudtrail.this.include_global_service_events
    log_file_validation      = aws_cloudtrail.this.enable_log_file_validation
    logging                  = aws_cloudtrail.this.enable_logging
    trail_kms_key_arn        = aws_cloudtrail.this.kms_key_id
    trail_bucket             = aws_cloudtrail.this.s3_bucket_name
    event_categories         = flatten([for s in aws_cloudtrail.this.advanced_event_selector : [for f in s.field_selector : f.equals if f.field == "eventCategory"]])
    data_event_prefixes      = flatten([for s in aws_cloudtrail.this.advanced_event_selector : [for f in s.field_selector : f.starts_with if f.field == "resources.ARN"]])
    trail_bucket_locked      = aws_s3_bucket.trail.object_lock_enabled
    trail_retention_mode     = one(one(aws_s3_bucket_object_lock_configuration.trail.rule).default_retention).mode
    trail_retention_days     = one(one(aws_s3_bucket_object_lock_configuration.trail.rule).default_retention).days
    trail_bucket_versioning  = one(aws_s3_bucket_versioning.trail.versioning_configuration).status
    config_bucket_versioning = one(aws_s3_bucket_versioning.config.versioning_configuration).status
    buckets_private = alltrue(flatten([
      for b in [aws_s3_bucket_public_access_block.trail, aws_s3_bucket_public_access_block.config] :
      [b.block_public_acls, b.block_public_policy, b.ignore_public_acls, b.restrict_public_buckets]
    ]))
    trail_bucket_deny_sids  = [for s in data.aws_iam_policy_document.trail_bucket.statement : s.sid if s.effect == "Deny"]
    config_bucket_deny_sids = [for s in data.aws_iam_policy_document.config_bucket.statement : s.sid if s.effect == "Deny"]
    # Every allow to the CloudTrail principal, with the trail it is scoped to.
    # An unscoped one shows up here as an empty list.
    cloudtrail_allow_source_arns = {
      for s in data.aws_iam_policy_document.trail_bucket.statement :
      s.sid => flatten([for c in s.condition : c.values if c.variable == "aws:SourceArn" && c.test == "StringEquals"])
      if s.effect != "Deny" && contains(flatten([for p in s.principals : tolist(p.identifiers)]), "cloudtrail.amazonaws.com")
    }
    log_group_kms_key_arn     = aws_cloudwatch_log_group.trail.kms_key_id
    log_group_retention_days  = aws_cloudwatch_log_group.trail.retention_in_days
    guardduty_enabled         = aws_guardduty_detector.this.enable
    config_records_all_types  = one(aws_config_configuration_recorder.this.recording_group).all_supported
    config_records_global     = one(aws_config_configuration_recorder.this.recording_group).include_global_resource_types
    config_recording          = aws_config_configuration_recorder_status.this.is_enabled
    config_delivery_bucket    = aws_config_delivery_channel.this.s3_bucket_name
    config_delivery_encrypted = aws_config_delivery_channel.this.s3_kms_key_arn
  }
}
