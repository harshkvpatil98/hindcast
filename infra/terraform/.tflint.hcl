# TFLint: rules `terraform validate` cannot check -- provider-aware values
# (instance classes, engine names), deprecated arguments, naming, and unused
# declarations. Pinned, like everything else in this repository.
config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.48.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
