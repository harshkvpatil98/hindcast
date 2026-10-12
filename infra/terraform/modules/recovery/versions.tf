terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
      # The replica lives in a second region, which is a second provider.
      configuration_aliases = [aws.replica]
    }
  }
}
