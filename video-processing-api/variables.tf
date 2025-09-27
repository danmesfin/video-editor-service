variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "ffmpeg_layer_zip_path" {
  description = "Local path to a prebuilt ffmpeg Lambda layer zip. If empty, layer is not created."
  type        = string
  default     = ""
}

variable "external_ffmpeg_layer_arn" {
  description = "Optional existing FFmpeg Lambda Layer ARN to attach (e.g., from SAR). If set, zip-based layer creation is skipped."
  type        = string
  default     = ""
}

variable "project" {
  description = "Project name used for resource naming"
  type        = string
  default     = "video-processing-api"
}

variable "env" {
  description = "Deployment environment suffix (e.g., dev, prod)"
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "private_subnet_a_cidr" {
  description = "CIDR block for private subnet A"
  type        = string
  default     = "10.0.1.0/24"
}

variable "private_subnet_b_cidr" {
  description = "CIDR block for private subnet B"
  type        = string
  default     = "10.0.2.0/24"
}

variable "public_subnet_a_cidr" {
  description = "CIDR block for public subnet A (for NAT Gateway)"
  type        = string
  default     = "10.0.10.0/24"
}
