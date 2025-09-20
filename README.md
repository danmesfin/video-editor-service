# Video editing service API (AWS Lambda + EFS)

A serverless video processing API that accepts videos and performs edits like merging, remuxing, or frame-by-frame modifications using FFmpeg (to-do).

## Architecture

- **API Gateway (HTTP API)**: Public endpoints `/`, `/process`, `/status/{job_id}`
- **Lambda (Processor)**: Python 3.11 with FFmpeg layer, runs inside VPC
- **SQS (Jobs Queue)**: Asynchronous job dispatch, Lambda consumes messages
- **S3 buckets**: Input and output buckets; job status JSON stored under `output-bucket/jobs/<job_id>/status.json`
- **EFS + Access Point**: Shared scratch storage for FFmpeg normalization and merge
- **VPC Endpoints**: S3 Gateway endpoint (for S3 access) and SQS Interface endpoint (for queue access)

```mermaid
flowchart TD
    A[Client]
    G[API Gateway]
    Q[SQS Queue]
    L[Lambda Processor]
    S[S3 Output]
    E[EFS Scratch]

    A --> G
    G --> Q
    Q --> L
    L --> E
    L --> S
    A --> G
    G --> S

    subgraph AWS
        G
        Q
        L
        S
        E
    end
```

## Deploy (Terraform)

## ER Diagram

```mermaid
erDiagram
    JOB ||--o{ MEDIA_ASSET : includes
    JOB ||--|| OUTPUT : produces
    JOB ||--o{ STATUS_EVENT : progresses

    JOB {
      string job_id PK
      string status
      number started_at
      number completed_at
    }

    MEDIA_ASSET {
      string url
      string type
      string source  "s3|http"
      number order_index
    }

    OUTPUT {
      string bucket
      string key
      string download_url
      number expires_at
    }

    STATUS_EVENT {
      string status  "queued|processing|downloading|merging|completed|failed"
      string message
      number timestamp
    }
```

### Prerequisites

1) **AWS Credentials**: Configure shared profile (recommended):
   ```bash
   # Create ~/.aws/credentials
   [default]
   aws_access_key_id = YOUR_ACCESS_KEY
   aws_secret_access_key = YOUR_SECRET_KEY
   
   # Create ~/.aws/config  
   [default]
   region = eu-north-1
   output = json
   ```

2) **FFmpeg Layer**: Choose one option:
   - **Option A (Recommended)**: Use AWS Serverless Application Repository layer
   - **Option B**: Build custom layer with static FFmpeg binary

### Project Structure

```
video-processing-api/
 ├─ main.tf              # Infrastructure resources
 ├─ variables.tf         # Configuration variables
 ├─ outputs.tf          # Stack outputs
 ├─ lambda/
 │   ├─ main.py         # Lambda handler
 │   └─ requirements.txt # Python dependencies
 └─ layers/
     └─ README.md       # Layer setup instructions
```

### Deployment Steps

1) **Initialize Terraform**:
   ```bash
   cd video-processing-api
   terraform init
   ```

2) **Deploy with External FFmpeg Layer** (Recommended):
   ```bash
   # Deploy SAR FFmpeg layer first (via AWS Console or CLI)
   # Then reference the layer ARN:
   terraform apply -auto-approve \
     -var="aws_region=eu-north-1" \
     -var="external_ffmpeg_layer_arn=arn:aws:lambda:eu-north-1:ACCOUNT:layer:ffmpeg:VERSION"
   ```

3) **Alternative: Deploy with Custom Layer**:
   ```bash
   # Place ffmpeg binary at layers/bin/ffmpeg, then:
   cd layers && zip -r ffmpeg-layer.zip bin/ && cd ..
   terraform apply -auto-approve \
     -var="aws_region=eu-north-1" \
     -var="ffmpeg_layer_zip_path=./layers/ffmpeg-layer.zip"
   ```

4) **Deploy without FFmpeg** (S3 copy only):
   ```bash
   terraform apply -auto-approve -var="aws_region=eu-north-1"
   ```

### API Usage

**Health Check**:
```bash
curl -s $(terraform output -raw api_endpoint)
# Expected: {"status":"ok","has_ffmpeg":true,"mount_path_exists":true}
```

**Video Merging** (Primary Use Case):
```bash
# Merge videos from URLs (async)
curl -s -X POST $(terraform output -raw api_endpoint)/process \
  -H 'content-type: application/json' \
  -d '{
    "operation": "merge",
    "video_urls": [
      "https://example.com/video1.mp4",
      "https://example.com/video2.mp4",
      "https://example.com/video3.mp4"
    ]
  }'

# Response (202 Accepted):
# {
#   "accepted": true,
#   "job_id": "a1b2c3d4",
#   "status_url": "https://<api>/status/a1b2c3d4"
# }
```

**Job Status Checking**:
```bash
# Check job progress (for frontend polling)
curl -s $(terraform output -raw api_endpoint)/status/a1b2c3d4

# Example response while running:
# {
#   "job_id": "a1b2c3d4",
#   "status": "merging",
#   "progress": 72.5,
#   "timestamp": "1695123456",
#   "metadata": {
#     "video_urls": ["..."],
#     "videos_count": 2,
#     "normalized": 1
#   }
# }

# When completed, includes a presigned download_url (valid ~1h):
# {
#   "job_id": "a1b2c3d4",
#   "status": "completed",
#   "progress": 100,
#   "metadata": {
#     "download_url": "https://s3.../merged/a1b2c3d4/output.mp4?..."
#   }
# }
```


### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check and API documentation |
| `POST` | `/process` | Merge videos or process single video |
| `GET` | `/status/{job_id}` | Check job status for frontend polling |

### Request Examples

**Merge Multiple Videos**:
```json
{
  "operation": "merge",
  "video_urls": [
    "https://example.com/video1.mp4",
    "https://example.com/video2.mp4"
  ]
}
```

**Response**:
```json
{
  "success": true,
  "job_id": "a1b2c3d4",
  "videos_merged": 2,
  "download_url": "https://s3.amazonaws.com/.../merged-video.mp4?signature=...",
  "expires_in": "1 hour"
}
```

**Job Status Response**:
```json
{
  "job_id": "a1b2c3d4",
  "status": "completed",
  "timestamp": "1695123456",
  "metadata": {
    "video_urls": ["https://example.com/video1.mp4", "https://example.com/video2.mp4"],
    "videos_count": 2,
    "download_url": "https://s3.amazonaws.com/.../merged-video.mp4",
    "completed_at": 1695123500
  }
}
```
---

## TODO

- [ ] Add support for frame-by-frame modifications
- [ ] Add support for video cropping
- [ ] Add support for video rotation
- [ ] Add support for video trimming
