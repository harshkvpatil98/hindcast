# The demonstration profile (ADR-0054). A minimal live deployment that proves
# the event-lake path -- S3, Glue and Athena under one CMK, with the lake's two
# roles -- and none of the production tiers. The defaults in main.tf are the
# platform as designed; nothing here removes a module from the repository.
#
#   cp portfolio.tfvars terraform.tfvars     # gitignored, and read automatically
#   terraform init -backend-config=backend.hcl
#   terraform plan -out=platform.tfplan
#
# Everything this profile creates is removed by one `terraform destroy`; the
# CMK then waits out its 30-day deletion window, unbilled.

region = "us-west-2"
# Required by the root, which configures a second provider for the recovery
# replica. With recovery off nothing is created there.
replica_region = "us-west-1"

# --- Tiers: what a demonstration does not need ------------------------------------

# No tier-0 recovery archive: no cross-region replication, no replica-region
# CMK, no S3 Inventory, no restore-drill role.
enable_recovery = false
# No reports bucket: nothing deployed here publishes one, and `cascade report`
# writes its directory locally. Set true to add a private, CMK-encrypted
# bucket and its read policy (7 resources, no standing cost).
enable_reports = false
# No Object Lock on the lake (or on the reports bucket, were it on). The lake
# keeps the writer role's explicit Deny, which is invariant 6's other control.
enable_object_lock = false
# No GuardDuty, no AWS Config: both bill on activity, and nobody operates this
# account day to day. See infra/terraform/README.md before turning either on.
enable_guardduty = false
enable_config    = false
# `terraform destroy` may empty the lake bucket, every version included.
disposable = true

# --- What the owner's account already has, made by hand --------------------------
# In a fresh account, delete these four lines: each then defaults to creating.
create_audit_trail              = false # `cascade-audit-trail` exists
create_budget                   = false # `cascade budget` exists
create_cost_anomaly_detection   = false # `Default-Services-Monitor` exists: one per account
create_service_control_policies = false # not an AWS Organizations management account

# --- Required decisions -------------------------------------------------------------
# The root has no default for these. In this profile each is read only by a
# tier that is off, so the values are placeholders for the day one is turned on.
infrastructure_allowance_usd = 50       # the budget
guardduty_min_severity       = 7        # GuardDuty
report_lock_retention_days   = 365      # the reports bucket's lock
inventory_schedule           = "Weekly" # recovery

# Nobody is emailed from the alerts topic. With the tiers above off nothing in
# this root publishes to it; the sandbox's alarms would.
alert_emails = []

# The Bedrock guardrail (`cascade-audit-guardrail`) stays outside Terraform:
# `model_guardrail` is not set, so nothing about it is planned. Its id and
# version go in .env (CASCADE_GUARDRAIL_ID, CASCADE_GUARDRAIL_VERSION=1).
