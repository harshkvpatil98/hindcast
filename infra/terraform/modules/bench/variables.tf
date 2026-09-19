variable "name" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "vpc_cidr_block" {
  type = string
}

variable "s3_prefix_list_id" {
  description = "The S3 gateway endpoint's prefix list, for egress to S3."
  type        = string
}

variable "kms_key_arn" {
  description = "Platform CMK: artifacts bucket, ECR, task logs."
  type        = string
}

variable "db_kms_key_arn" {
  description = "The database CMK, which encrypts the secrets the task reads."
  type        = string
}

variable "db_security_group_id" {
  type = string
}

variable "db_endpoint" {
  type = string
}

variable "db_port" {
  type = number
}

variable "db_name" {
  type = string
}

variable "db_cluster_resource_id" {
  type = string
}

variable "master_user_secret_arn" {
  type = string
}

variable "role_secret_arns" {
  description = "Secret ARN per Postgres role, from the database module."
  type        = map(string)
}

variable "artifacts_bucket_name" {
  description = "Bucket for the corpus dump. Named by the caller so the S3 endpoint policy can allow it before it exists."
  type        = string
}

variable "image_tag" {
  description = "Tag of the bench image in ECR. Tags are immutable, so a tag names exactly one image."
  type        = string
  default     = "m11"
}

variable "task_cpu" {
  description = "Fargate vCPU units. The bench is single-connection, so CPU matters less than being in-region."
  type        = number
  default     = 4096
}

variable "task_memory" {
  description = "MiB. Holds the embedding model and the restore's working set."
  type        = number
  default     = 16384
}

variable "ephemeral_storage_gib" {
  description = "Scratch for the corpus dump during restore (~8 GiB at 1.95M chunks)."
  type        = number
  default     = 50
}

variable "disposable" {
  description = <<-EOT
    Let `terraform destroy` remove the image repository and the artifacts
    bucket while they still hold images and objects. Both are reproducible --
    images from the code, the dump from the corpus -- so a sandbox sets this;
    the default keeps them safe from an accidental destroy.
  EOT
  type        = bool
  default     = false
}

variable "log_retention_days" {
  type    = number
  default = 30
}
