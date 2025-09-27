# Video Editing Service API (AWS Lambda + EFS)

A serverless video processing API that merges and edits videos using FFmpeg.

## Key Features

- **Merge multiple videos** asynchronously with progress tracking
- **Caption/text overlay** 
- **Add audio overlay** 
- **Image watermark overlay** with configurable position, opacity, and scale
- **Video overlay** with position, size, start time, and duration
- **Presigned download URLs** with status persistence in S3
- **Local development harness** via FastAPI wrapper + LocalStack S3/SQS

## Architecture Overview

- **API Gateway (HTTP API)** routes requests to processor Lambda
- **Lambda (Python 3.11 + FFmpeg)** runs inside VPC with EFS scratch storage
- **SQS queue** for async job dispatch; Lambda consumes messages
- **S3 buckets** for inputs, outputs, and job status JSON
- **EFS** provides shared scratch space during normalization/merge
- **VPC Endpoints** (S3/SQS) + **NAT Gateway** for internet egress

## Quick Start (Local)

### Prerequisites
- Docker and docker-compose

### Steps
1. **Start LocalStack and API**:
   ```bash
   cp local.env.example .env.local
   docker compose up -d localstack
   docker compose up -d --build api
   ```

2. **Health check**:
   ```bash
   curl -s http://localhost:8000 | jq
   ```

3. **Submit a merge job**:
   ```bash
   curl -s -X POST http://localhost:8000/process \
     -H 'content-type: application/json' \
     -d '{
       "operation": "merge",
       "video_urls": [
         "https://samplelib.com/lib/preview/mp4/sample-5s.mp4",
         "https://samplelib.com/lib/preview/mp4/sample-10s.mp4"
       ]
     }' | jq
   ```

4. **Poll status and download**:
   ```bash
   JOB_ID=$(curl -s http://localhost:8000/process -H 'content-type: application/json' -d '{"operation":"merge","video_urls":["https://samplelib.com/lib/preview/mp4/sample-5s.mp4"]}' | jq -r '.job_id // empty')
   curl -s "http://localhost:8000/status/$JOB_ID" | jq
   
   # Download when completed (uses download_url_local for localhost:4566)
   DL=$(curl -s "http://localhost:8000/status/$JOB_ID" | jq -r '.metadata.download_url_local // .metadata.download_url // empty')
   [ -n "$DL" ] && curl -L "$DL" -o result.mp4
   ```

### Local Development Notes
- Local API auto-discovers `QUEUE_URL` by `QUEUE_NAME` and rewrites host to `localstack:4566` inside container
- Responses include `download_url_local` (rewritten to `http://localhost:4566`) for easy host downloads
- Use public HTTPS URLs for inputs; LocalStack S3 inputs work via public links

## Cloud Deployment

### Prerequisites
- AWS CLI configured with appropriate permissions
- Terraform >= 1.5.0
- FFmpeg layer (external ARN recommended)

### Steps

1. **Initialize Terraform**:
   ```bash
   cd video-processing-api
   terraform init
   ```

2. **Deploy with external FFmpeg layer** (recommended):
   ```bash
   terraform apply -auto-approve \
     -var="aws_region=eu-north-1" \
     -var="external_ffmpeg_layer_arn=arn:aws:lambda:eu-north-1:920631856317:layer:ffmpeg:1"
   ```

3. **Get API endpoint**:
   ```bash
   API=$(terraform output -raw api_endpoint)
   echo "API endpoint: $API"
   ```

### Outputs
- `api_endpoint` - Base URL for API calls
- `s3_input_bucket` - Input bucket name
- `s3_output_bucket` - Output bucket name
- `lambda_name` - Lambda function name for monitoring

## API Reference

### Base URL
Use the `api_endpoint` from Terraform outputs or `http://localhost:8000` for local development.

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check and API info |
| `POST` | `/process` | Submit video processing operation |
| `GET` | `/status/{job_id}` | Poll job status and get download URL |

### Operations

#### Merge Videos
```json
{
  "operation": "merge",
  "video_urls": [
    "https://example.com/video1.mp4",
    "https://example.com/video2.mp4"
  ]
}
```

#### Add Caption/Text Overlay
```json
{
  "operation": "caption",
  "input": {"url": "https://example.com/video.mp4"},
  "caption": {
    "text": "Hello World!",
    "size": 36,
    "color": "white",
    "position": {"x": 50, "y": 90}
  }
}
```
**Position options:**
- String: `"top"`, `"bottom"`, `"center"`
- Percentage object: `{"x": 0-100, "y": 0-100}` (new feature)

#### Add Audio Overlay
```json
{
  "operation": "add-audio",
  "input": {"url": "https://example.com/video.mp4"},
  "audio": {
    "url": "https://example.com/audio.wav",
    "volume": 0.8,
    "start": 0
  }
}
```

#### Add Watermark
```json
{
  "operation": "watermark",
  "input": {"url": "https://example.com/video.mp4"},
  "watermark": {
    "url": "https://example.com/logo.png",
    "position": "top-right",
    "opacity": 0.8,
    "scale": 0.1
  }
}
```

#### Video Overlay
```json
{
  "operation": "overlay",
  "input": {"url": "https://example.com/main-video.mp4"},
  "overlay": {
    "url": "https://example.com/overlay-video.mp4",
    "position": {"x": 30, "y": 30},
    "size": {"width": 320, "height": 180},
    "start": 5,
    "duration": 10
  }
}
```

### Response Format

#### Async Response (202 Accepted)
```json
{
  "accepted": true,
  "job_id": "a1b2c3d4",
  "status_url": "https://api-endpoint/status/a1b2c3d4"
}
```

#### Status Response
```json
{
  "job_id": "a1b2c3d4",
  "status": "completed",
  "progress": 100,
  "timestamp": "1695123456",
  "metadata": {
    "download_url": "https://s3.../output.mp4?...",
    "download_url_local": "http://localhost:4566/..."
  }
}
```

## Behavior & Conventions

### Video Processing
- **Output format**: H.264/AAC with `yuv420p` pixel format for broad compatibility
- **Audio handling**: Silent stereo track injected when input lacks audio to maintain stream consistency
- **Normalization**: Videos normalized to consistent format before merging

### Progress Tracking
- **Status values**: `queued`, `processing`, `downloading`, `merging`, `uploading`, `completed`, `failed`
- **Progress**: 0-100 percentage with operation-specific metadata
- **Persistence**: Status stored as JSON in S3 output bucket under `jobs/{job_id}/status.json`

### Media Compatibility
- **Supported inputs**: MP4, MOV, AVI, MKV (H.264/H.265 preferred)
- **Audio formats**: AAC, MP3, WAV
- **Image formats**: PNG, JPG, GIF (for watermarks)

## Examples

The `examples/` directory contains helper scripts:

- `send_merge_request.sh` - Submit merge job
- `poll_status.sh` - Poll job status until completion
- `download_result.sh` - Download completed result

### Usage
```bash
cd examples
./send_merge_request.sh "https://example.com/video1.mp4" "https://example.com/video2.mp4"
# Returns job_id
./poll_status.sh "job_id_here"
./download_result.sh "job_id_here" "output.mp4"
```


## Roadmap

### Planned Features
- [ ] Video trimming/cutting operations
- [ ] Frame-by-frame modifications
- [ ] Video rotation and cropping
- [ ] Batch processing endpoints
- [ ] Webhook notifications for job completion
- [ ] Custom FFmpeg filter support

### Performance Improvements
- [ ] Parallel processing for merge operations
- [ ] Smart caching of normalized videos
- [ ] Lambda memory optimization based on video size
- [ ] EFS cleanup automation
