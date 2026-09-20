terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
  }
  # Local state, deliberately: this root creates the bucket every other root
  # keeps its state in. It holds no secrets -- a bucket, a key and a policy.
}
