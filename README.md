# Video editing service API (AWS Lambda + EFS)

an API that accepts videos and performs edits like merging or frame-by-frame modifications

## Architecture

- S3 bucket (for input + output videos)
- EFS + access point (for Lambda temp storage)
- Lambda function (with FFmpeg layer attached)
- API Gateway HTTP API (to trigger Lambda)

## TODO

- deploy the service on AWS 
