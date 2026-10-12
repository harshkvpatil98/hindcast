provider "aws" {
  # No default: the region decides where spend lands, and ADR-0028's rule is
  # that routing is explicit, never inherited from someone's shell.
  region = var.region

  default_tags {
    tags = {
      Project     = "cascade"
      Environment = var.name
      Milestone   = "M11"
      ManagedBy   = "terraform"
    }
  }
}
