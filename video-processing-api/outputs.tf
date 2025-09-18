output "api_endpoint" {
  description = "HTTP API invoke URL"
  value       = aws_apigatewayv2_api.http.api_endpoint
}

output "lambda_name" {
  description = "Lambda function name"
  value       = aws_lambda_function.processor.function_name
}

output "s3_input_bucket" {
  description = "Input S3 bucket"
  value       = aws_s3_bucket.input.bucket
}

output "s3_output_bucket" {
  description = "Output S3 bucket"
  value       = aws_s3_bucket.output.bucket
}

output "efs_file_system_id" {
  description = "EFS file system ID"
  value       = aws_efs_file_system.this.id
}

output "efs_access_point_arn" {
  description = "EFS access point ARN"
  value       = aws_efs_access_point.ap.arn
}
