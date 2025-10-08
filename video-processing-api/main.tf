terraform {
  required_version = ">= 1.5.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
# Use hardcoded AZs for eu-north-1 to avoid needing ec2:DescribeAvailabilityZones permission
locals {
  availability_zones = ["eu-north-1a", "eu-north-1b", "eu-north-1c"]
}

# -----------------------------
# Networking (VPC for Lambda + EFS)
# -----------------------------
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = { Name = "${var.project}-vpc" }
}

resource "aws_subnet" "private_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.private_subnet_a_cidr
  availability_zone       = local.availability_zones[0]
  map_public_ip_on_launch = false
  tags = { Name = "${var.project}-private-a" }
}

resource "aws_subnet" "private_b" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.private_subnet_b_cidr
  availability_zone       = local.availability_zones[1]
  map_public_ip_on_launch = false
  tags = { Name = "${var.project}-private-b" }
}

# S3 Gateway Endpoint so Lambda in private subnets can reach S3
resource "aws_vpc_endpoint" "s3" {
  vpc_id          = aws_vpc.main.id
  service_name    = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids = [
    aws_route_table.private_a.id,
    aws_route_table.private_b.id
  ]
  tags = { Name = "${var.project}-s3-endpoint" }
}

# Interface Endpoint Security Group (for VPC endpoints like SQS)
resource "aws_security_group" "vpc_endpoints" {
  name        = "${var.project}-vpc-endpoints-sg"
  description = "Allow HTTPS from Lambda to VPC interface endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    description      = "HTTPS from Lambda SG"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    security_groups  = [aws_security_group.lambda.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# SQS Interface Endpoint so Lambda in private subnets can reach SQS
resource "aws_vpc_endpoint" "sqs_interface" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.sqs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private_a.id, aws_subnet.private_b.id]
  security_group_ids  = [aws_security_group.vpc_endpoints.id]
  private_dns_enabled = true
  tags = { Name = "${var.project}-sqs-endpoint" }
}

resource "aws_route_table" "private_a" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-rt-private-a" }
}

resource "aws_route_table" "private_b" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-rt-private-b" }
}

resource "aws_route_table_association" "a" {
  subnet_id      = aws_subnet.private_a.id
  route_table_id = aws_route_table.private_a.id
}

resource "aws_route_table_association" "b" {
  subnet_id      = aws_subnet.private_b.id
  route_table_id = aws_route_table.private_b.id
}

# -----------------------------
# Public Subnet + Internet/NAT egress
# -----------------------------
resource "aws_subnet" "public_a" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_a_cidr
  availability_zone       = local.availability_zones[0]
  map_public_ip_on_launch = true
  tags = { Name = "${var.project}-public-a" }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-igw" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${var.project}-nat-eip" }
}

resource "aws_nat_gateway" "ngw" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public_a.id
  tags          = { Name = "${var.project}-nat" }
  depends_on    = [aws_internet_gateway.igw]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.project}-rt-public" }
}

resource "aws_route" "public_internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.igw.id
}

resource "aws_route_table_association" "public_a" {
  subnet_id      = aws_subnet.public_a.id
  route_table_id = aws_route_table.public.id
}

# Default routes for private subnets via NAT
resource "aws_route" "private_a_default" {
  route_table_id         = aws_route_table.private_a.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.ngw.id
}

resource "aws_route" "private_b_default" {
  route_table_id         = aws_route_table.private_b.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.ngw.id
}

# -----------------------------
# EFS + Security Groups
# -----------------------------
resource "aws_security_group" "lambda" {
  name        = "${var.project}-lambda-sg"
  description = "Security group for Lambda"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "efs" {
  name        = "${var.project}-efs-sg"
  description = "Security group for EFS"
  vpc_id      = aws_vpc.main.id

  ingress {
    description      = "NFS from Lambda"
    from_port        = 2049
    to_port          = 2049
    protocol         = "tcp"
    security_groups  = [aws_security_group.lambda.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_efs_file_system" "this" {
  creation_token = "${var.project}-efs"
  tags           = { Name = "${var.project}-efs" }
}

resource "aws_efs_mount_target" "a" {
  file_system_id  = aws_efs_file_system.this.id
  subnet_id       = aws_subnet.private_a.id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_mount_target" "b" {
  file_system_id  = aws_efs_file_system.this.id
  subnet_id       = aws_subnet.private_b.id
  security_groups = [aws_security_group.efs.id]
}

resource "aws_efs_access_point" "ap" {
  file_system_id = aws_efs_file_system.this.id

  posix_user {
    gid = 1000
    uid = 1000
  }

  root_directory {
    path = "/lambda"
    creation_info {
      owner_gid   = 1000
      owner_uid   = 1000
      permissions = "0755"
    }
  }
}

# -----------------------------
# S3 Buckets
# -----------------------------
resource "aws_s3_bucket" "input" {
  bucket = "${var.project}-input-${var.env}-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}"
  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "Content-Length", "Content-Type"]
    max_age_seconds = 86400
  }
}

resource "aws_s3_bucket" "output" {
  bucket = "${var.project}-output-${var.env}-${data.aws_caller_identity.current.account_id}-${data.aws_region.current.name}"
  cors_rule {
    allowed_methods = ["GET", "HEAD"]
    allowed_origins = ["*"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "Content-Length", "Content-Type"]
    max_age_seconds = 86400
  }
}

resource "aws_s3_bucket_public_access_block" "output" {
  bucket = aws_s3_bucket.output.id

  block_public_acls       = false
  block_public_policy     = false
  ignore_public_acls      = false
  restrict_public_buckets = false
}

resource "aws_s3_bucket_policy" "output_public_read" {
  bucket = aws_s3_bucket.output.id
  depends_on = [aws_s3_bucket_public_access_block.output]

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicReadGetObject"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.output.arn}/*"
      }
    ]
  })
}

# -----------------------------
# IAM for Lambda
# -----------------------------
resource "aws_iam_role" "lambda_exec" {
  name = "${var.project}-${var.env}-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = "sts:AssumeRole",
      Effect = "Allow",
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "basic" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy_attachment" "efs_client" {
  role       = aws_iam_role.lambda_exec.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonElasticFileSystemClientReadWriteAccess"
}

resource "aws_iam_role_policy" "s3_access" {
  name = "${var.project}-${var.env}-s3-access"
  role = aws_iam_role.lambda_exec.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Effect = "Allow",
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket"
        ],
        Resource = [
          aws_s3_bucket.input.arn,
          "${aws_s3_bucket.input.arn}/*",
          aws_s3_bucket.output.arn,
          "${aws_s3_bucket.output.arn}/*"
        ]
      },
      {
        Effect = "Allow",
        Action = [
          "sqs:SendMessage",
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:GetQueueUrl"
        ],
        Resource = [
          aws_sqs_queue.jobs.arn
        ]
      },
      {
        Effect = "Allow",
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
        Resource = "*"
      }
    ]
  })
}

# Optional: FFmpeg layer (provide zip path to enable or external ARN)

locals {
  # Whether a local zip should be used to create a layer
  use_local_layer_zip = length(var.ffmpeg_layer_zip_path) > 0 && length(var.external_ffmpeg_layer_arn) == 0
  # The chosen layer ARN (external takes precedence)
  chosen_ffmpeg_layer_arn = length(var.external_ffmpeg_layer_arn) > 0 ? var.external_ffmpeg_layer_arn : (local.use_local_layer_zip ? aws_lambda_layer_version.ffmpeg[0].arn : "")
}

data "archive_file" "lambda_zip" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/lambda.zip"
}

resource "aws_lambda_layer_version" "ffmpeg" {
  count               = local.use_local_layer_zip ? 1 : 0
  filename            = var.ffmpeg_layer_zip_path
  layer_name          = "${var.project}-ffmpeg"
  compatible_runtimes = ["python3.11", "python3.10"]
}

resource "aws_lambda_function" "processor" {
  function_name = "${var.project}-${var.env}"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "main.handler"
  runtime       = "python3.11"

  filename         = data.archive_file.lambda_zip.output_path
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256

  timeout = 900
  memory_size = 3008

  layers = length(local.chosen_ffmpeg_layer_arn) > 0 ? [local.chosen_ffmpeg_layer_arn] : []

  vpc_config {
    subnet_ids         = [aws_subnet.private_a.id, aws_subnet.private_b.id]
    security_group_ids = [aws_security_group.lambda.id]
  }

  file_system_config {
    arn              = aws_efs_access_point.ap.arn
    local_mount_path = "/mnt/efs"
  }

  environment {
    variables = {
      INPUT_BUCKET  = aws_s3_bucket.input.bucket
      OUTPUT_BUCKET = aws_s3_bucket.output.bucket
      MOUNT_PATH    = "/mnt/efs"
      QUEUE_URL     = aws_sqs_queue.jobs.url
      AWS_REGION    = data.aws_region.current.name
    }
  }
}

# -----------------------------
# SQS for async processing
# -----------------------------
resource "aws_sqs_queue" "jobs" {
  name                        = "${var.project}-${var.env}-jobs"
  visibility_timeout_seconds  = 900
  message_retention_seconds   = 86400
}

resource "aws_lambda_event_source_mapping" "sqs_trigger" {
  event_source_arn = aws_sqs_queue.jobs.arn
  function_name    = aws_lambda_function.processor.arn
  batch_size       = 1
  enabled          = true
}

# -----------------------------
# API Gateway HTTP API -> Lambda
# -----------------------------
resource "aws_apigatewayv2_api" "http" {
  name          = "${var.project}-${var.env}-api"
  protocol_type = "HTTP"

  # Enable CORS for browser clients (React app)
  cors_configuration {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "OPTIONS"]
    allow_headers     = ["content-type", "authorization"]
    expose_headers    = ["content-type"]
    max_age           = 86400
    allow_credentials = false
  }
}

resource "aws_apigatewayv2_integration" "lambda" {
  api_id                 = aws_apigatewayv2_api.http.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.processor.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "process" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "POST /process"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_route" "status" {
  api_id    = aws_apigatewayv2_api.http.id
  route_key = "GET /status/{job_id}"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.http.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.processor.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.http.execution_arn}/*/*"
}
