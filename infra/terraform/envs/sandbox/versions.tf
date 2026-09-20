terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0, < 7.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6, < 4.0"
    }
  }

  # Partial configuration: `terraform init -backend-config=backend.hcl`, from
  # backend.hcl.example. State holds generated role passwords, so it lives in
  # the encrypted, versioned bucket envs/bootstrap creates, with native S3
  # locking (use_lockfile) rather than a DynamoDB table.
  backend "s3" {}
}
