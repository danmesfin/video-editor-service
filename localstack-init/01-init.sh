#!/usr/bin/env bash
set -euo pipefail

# Use awslocal (provided by LocalStack) to create resources
: "${AWS_DEFAULT_REGION:=eu-north-1}"

INPUT_BUCKET=${INPUT_BUCKET:-video-api-local-input}
OUTPUT_BUCKET=${OUTPUT_BUCKET:-video-api-local-output}
QUEUE_NAME=${QUEUE_NAME:-video-api-local-jobs}

echo "[init] Creating S3 buckets: $INPUT_BUCKET, $OUTPUT_BUCKET"
awslocal s3 mb "s3://$INPUT_BUCKET" || true
awslocal s3 mb "s3://$OUTPUT_BUCKET" || true

# Enable public ACLs for simplicity in local
awslocal s3api put-bucket-acl --bucket "$INPUT_BUCKET" --acl public-read || true
awslocal s3api put-bucket-acl --bucket "$OUTPUT_BUCKET" --acl public-read || true

echo "[init] Creating SQS queue: $QUEUE_NAME"
awslocal sqs create-queue --queue-name "$QUEUE_NAME" >/dev/null || true

echo "[init] Done"
