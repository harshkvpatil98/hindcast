# Deployment identity: GitHub Actions assumes a role through OIDC, so no AWS
# key is ever stored in the repository's secrets. A stored key is long-lived,
# works from anywhere, and leaks in logs; an OIDC token lives for one job and
# is only good from the workflow the trust policy names.
#
# Two roles, because reading and changing are different trust decisions:
#   plan  -- any pull request in the repository may READ the account and the
#            state, to show what a change would do;
#   apply -- only a job in the named deployment environment may CHANGE it, and
#            an environment can require a human reviewer.

locals {
  issuer = "token.actions.githubusercontent.com"
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://${local.issuer}"
  client_id_list = ["sts.amazonaws.com"]
}

# --- plan: read-only, from pull requests ------------------------------------------

data "aws_iam_policy_document" "plan_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
    # StringEquals, not StringLike: the subject is matched whole, so a fork or
    # another repository cannot satisfy it with a lookalike suffix.
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:sub"
      values   = ["repo:${var.github_repository}:pull_request"]
    }
  }
}

resource "aws_iam_role" "plan" {
  name                 = "${var.name}-terraform-plan"
  description          = "terraform plan from pull requests: read-only"
  assume_role_policy   = data.aws_iam_policy_document.plan_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "plan_read_only" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "state_read" {
  statement {
    sid       = "ReadState"
    actions   = ["s3:GetObject", "s3:ListBucket"]
    resources = [var.state_bucket_arn, "${var.state_bucket_arn}/*"]
  }
  statement {
    sid       = "DecryptState"
    actions   = ["kms:Decrypt"]
    resources = [var.state_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "plan_state" {
  name   = "read-state"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.state_read.json
}

# --- apply: from the reviewed environment only ----------------------------------------

data "aws_iam_policy_document" "apply_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.issuer}:sub"
      values   = ["repo:${var.github_repository}:environment:${var.apply_environment}"]
    }
  }
}

resource "aws_iam_role" "apply" {
  name                 = "${var.name}-terraform-apply"
  description          = "terraform apply from the reviewed deployment environment"
  assume_role_policy   = data.aws_iam_policy_document.apply_trust.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "apply" {
  for_each   = toset(var.apply_policy_arns)
  role       = aws_iam_role.apply.name
  policy_arn = each.value
}

data "aws_iam_policy_document" "state_write" {
  statement {
    sid = "ReadWriteState"
    # PutObject/DeleteObject also cover the native S3 lock file (use_lockfile).
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = [var.state_bucket_arn, "${var.state_bucket_arn}/*"]
  }
  statement {
    sid       = "StateKey"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [var.state_kms_key_arn]
  }
}

resource "aws_iam_role_policy" "apply_state" {
  name   = "read-write-state"
  role   = aws_iam_role.apply.id
  policy = data.aws_iam_policy_document.state_write.json
}
