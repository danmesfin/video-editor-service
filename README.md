# Video editing service API (AWS Lambda + EFS)

an API that accepts videos and performs edits like merging or frame-by-frame modifications

## Architecture

- S3 bucket (for input + output videos)
- EFS + access point (for Lambda temp storage)
- Lambda function (with FFmpeg layer attached)
- API Gateway HTTP API (to trigger Lambda)

graph TD
    A[User / Client] -->|Upload Video| B[S3 Bucket: uploads/]
    A -->|Trigger Processing| G[API Gateway]

    G -->|Invoke| H[Lambda Function]

    H -->|Read Input Video| B
    H -->|Mount| I[EFS File System]

    H -->|Process Video with FFmpeg / OpenCV| I
    H -->|Write Output Video| I

    H -->|Upload Result| B

    A <-->|Download Result| B

    subgraph AWS
        B[S3 Bucket]
        H[Lambda Function]
        I[EFS Storage]
        G[API Gateway]
    end


## Deploy (Terraform)

Project layout:

```
video-processing-api/
 ├─ main.tf
 ├─ variables.tf
 ├─ outputs.tf
 ├─ lambda/
 │   ├─ main.py
 │   └─ requirements.txt
 └─ layers/
     └─ ffmpeg-layer.zip   # optional prebuilt layer zip with ffmpeg binary
```

Steps:

1) Ensure AWS credentials are configured (AWS CLI or env vars: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`).

2) Optionally add an FFmpeg layer:
   - Place your prebuilt zip at `video-processing-api/layers/ffmpeg-layer.zip`.
   - The zip should contain `bin/ffmpeg` (executable). On Lambda it becomes available at `/opt/bin/ffmpeg`.

3) Initialize and deploy:

```
cd video-processing-api
terraform init
terraform plan -var="env=dev" \
  -var="aws_region=us-east-1" \
  -var="ffmpeg_layer_zip_path=./layers/ffmpeg-layer.zip"   # omit or empty to deploy without layer
terraform apply -auto-approve -var="env=dev" -var="aws_region=us-east-1" -var="ffmpeg_layer_zip_path=./layers/ffmpeg-layer.zip"
```

Outputs include the `api_endpoint`. Test the health check:

```
curl -s $(terraform output -raw api_endpoint)
```

Invoke processing (copy/remux example):

```
curl -X POST $(terraform output -raw api_endpoint)/process \
  -H 'content-type: application/json' \
  -d '{
        "input_bucket": "<your-input-bucket>",
        "input_key": "path/to/input.mp4",
        "output_bucket": "<your-output-bucket>",
        "output_key": "processed/output.mp4"
      }'
```

Notes:
- Without the ffmpeg layer, the Lambda performs an S3 copy. With ffmpeg present, it attempts a container-copy remux using EFS as scratch.
- Buckets are created per-account/region with unique names output by Terraform.
