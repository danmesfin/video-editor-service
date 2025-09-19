import json
import os
import tempfile
import subprocess
from pathlib import Path
import base64
import urllib.request
import uuid
import time
from urllib.parse import urlparse

import boto3

s3 = boto3.client("s3")

INPUT_BUCKET = os.getenv("INPUT_BUCKET")
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET")
MOUNT_PATH = os.getenv("MOUNT_PATH", "/mnt/efs")


def _has_ffmpeg() -> str | None:
    candidates = [
        "/opt/bin/ffmpeg",
        "/opt/ffmpeg",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _download_video_from_url(url: str, output_path: str) -> None:
    """Download video from URL to local path"""
    with urllib.request.urlopen(url) as response:
        with open(output_path, 'wb') as f:
            f.write(response.read())


def _generate_presigned_url(bucket: str, key: str, expiration: int = 3600) -> str:
    """Generate presigned URL for S3 object download"""
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=expiration
    )


def _save_job_status(job_id: str, status: str, metadata: dict = None):
    """Save job status to S3 for tracking"""
    status_data = {
        "job_id": job_id,
        "status": status,
        "timestamp": str(int(time.time())),
        "metadata": metadata or {}
    }
    status_key = f"jobs/{job_id}/status.json"
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=status_key,
        Body=json.dumps(status_data),
        ContentType="application/json"
    )


def _get_job_status(job_id: str):
    """Get job status from S3"""
    try:
        status_key = f"jobs/{job_id}/status.json"
        response = s3.get_object(Bucket=OUTPUT_BUCKET, Key=status_key)
        return json.loads(response['Body'].read().decode('utf-8'))
    except s3.exceptions.NoSuchKey:
        return None
    except Exception:
        return None


def _handle_merge_operation(data):
    """Handle video merging operation with URL inputs"""
    video_urls = data.get("video_urls", [])  # List of video URLs
    
    if not video_urls or len(video_urls) < 2:
        return {
            "statusCode": 400,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({
                "error": "Merge operation requires at least 2 video URLs",
                "example": {
                    "operation": "merge",
                    "video_urls": [
                        "https://example.com/video1.mp4",
                        "https://example.com/video2.mp4"
                    ]
                }
            }),
        }
    
    ffmpeg_path = _has_ffmpeg()
    if not ffmpeg_path:
        return {
            "statusCode": 400,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({
                "error": "FFmpeg not available - merge operation requires FFmpeg layer",
            }),
        }
    
    try:
        # Generate unique job ID for this merge
        job_id = str(uuid.uuid4())[:8]
        
        # Save initial job status
        _save_job_status(job_id, "processing", {
            "video_urls": video_urls,
            "videos_count": len(video_urls),
            "started_at": time.time()
        })
        
        tmp_dir = Path(MOUNT_PATH) / f"merge_{job_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        
        # Update status: downloading
        _save_job_status(job_id, "downloading", {
            "video_urls": video_urls,
            "videos_count": len(video_urls)
        })
        
        # Download all videos from URLs
        input_paths = []
        for i, url in enumerate(video_urls):
            # Get file extension from URL or default to .mp4
            parsed_url = urlparse(url)
            ext = Path(parsed_url.path).suffix or '.mp4'
            input_path = tmp_dir / f"input_{i}{ext}"
            
            _download_video_from_url(url, str(input_path))
            input_paths.append(str(input_path))
        
        # Update status: merging
        _save_job_status(job_id, "merging", {
            "video_urls": video_urls,
            "videos_count": len(video_urls)
        })
        
        # Create concat file for FFmpeg
        concat_file = tmp_dir / "concat.txt"
        with open(concat_file, "w") as f:
            for path in input_paths:
                f.write(f"file '{path}'\n")
        
        output_path = tmp_dir / "merged_output.mp4"
        
        # Two-pass approach: normalize all videos first, then concatenate
        normalized_paths = []
        
        # Step 1: Normalize each video to common format
        for i, input_path in enumerate(input_paths):
            normalized_path = tmp_dir / f"normalized_{i}.mp4"
            
            # Normalize to common specs: 1080p, 30fps, H.264, AAC
            subprocess.run([
                ffmpeg_path, "-y", "-i", input_path,
                "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                "-r", "30", "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                str(normalized_path)
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            normalized_paths.append(str(normalized_path))
        
        # Step 2: Create new concat file with normalized videos
        normalized_concat_file = tmp_dir / "normalized_concat.txt"
        with open(normalized_concat_file, "w") as f:
            for path in normalized_paths:
                f.write(f"file '{path}'\n")
        
        # Step 3: Concatenate normalized videos (stream copy now safe)
        subprocess.run([
            ffmpeg_path, "-y", "-f", "concat", "-safe", "0",
            "-i", str(normalized_concat_file), "-c", "copy",
            str(output_path)
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # Upload result to S3 with unique key
        output_key = f"merged/{job_id}/output.mp4"
        s3.upload_file(str(output_path), OUTPUT_BUCKET, output_key)
        
        # Generate download URL (valid for 1 hour)
        download_url = _generate_presigned_url(OUTPUT_BUCKET, output_key, 3600)
        
        # Update status: completed
        _save_job_status(job_id, "completed", {
            "video_urls": video_urls,
            "videos_count": len(video_urls),
            "download_url": download_url,
            "completed_at": time.time()
        })
        
        return {
            "statusCode": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({
                "success": True,
                "job_id": job_id,
                "videos_merged": len(video_urls),
                "download_url": download_url,
                "expires_in": "1 hour"
            }),
        }
        
    except Exception as e:
        # Update status: failed
        try:
            _save_job_status(job_id, "failed", {
                "error": str(e),
                "failed_at": time.time()
            })
        except:
            pass  # Don't fail if we can't save status
        
        return {
            "statusCode": 500,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({
                "error": f"Merge operation failed: {str(e)}",
                "operation": "merge"
            }),
        }


def _handle_remux_operation(data):
    """Handle single video remux/copy operation"""
    input_bucket = data.get("input_bucket") or INPUT_BUCKET
    input_key = data.get("input_key")
    output_bucket = data.get("output_bucket") or OUTPUT_BUCKET
    output_key = data.get("output_key") or (f"processed/{input_key}" if input_key else None)

    if not input_bucket or not input_key or not output_bucket or not output_key:
        return {
            "statusCode": 400,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({
                "error": "Missing required fields: input_bucket, input_key, output_bucket, output_key",
            }),
        }

    # Minimal baseline: copy input object to output bucket/key.
    # If ffmpeg is present, we demonstrate a trivial remux operation (copy codec) using EFS as scratch.
    ffmpeg_path = _has_ffmpeg()
    if ffmpeg_path:
        # Download to EFS scratch
        tmp_dir = Path(MOUNT_PATH) / "work"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        in_path = tmp_dir / "input"
        out_path = tmp_dir / "output.mp4"

        s3.download_file(input_bucket, input_key, str(in_path))

        # Run ffmpeg remux (no re-encode, fastest). If container unsupported, it will fail; fallback to copy.
        try:
            subprocess.run(
                [ffmpeg_path, "-y", "-i", str(in_path), "-c", "copy", str(out_path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            s3.upload_file(str(out_path), output_bucket, output_key)
            result = {
                "operation": "ffmpeg_copy",
                "input": {"bucket": input_bucket, "key": input_key},
                "output": {"bucket": output_bucket, "key": output_key},
            }
        except Exception as e:
            # Fallback to server-side copy if ffmpeg failed
            s3.copy({"Bucket": input_bucket, "Key": input_key}, output_bucket, output_key)
            result = {
                "operation": "s3_copy_fallback",
                "error": str(e),
                "input": {"bucket": input_bucket, "key": input_key},
                "output": {"bucket": output_bucket, "key": output_key},
            }
    else:
        # No ffmpeg available: just copy in S3
        s3.copy({"Bucket": input_bucket, "Key": input_key}, output_bucket, output_key)
        result = {
            "operation": "s3_copy",
            "input": {"bucket": input_bucket, "key": input_key},
            "output": {"bucket": output_bucket, "key": output_key},
        }

    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(result),
    }


def handler(event, context):
    try:
        method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

        if method == "GET":
            return {
                "statusCode": 200,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({
                    "status": "ok",
                    "message": "Video Merge API - Ready to merge videos from URLs",
                    "has_ffmpeg": _has_ffmpeg() is not None,
                    "mount_path_exists": Path(MOUNT_PATH).exists(),
                    "usage": {
                        "endpoint": "POST /process",
                        "example": {
                            "operation": "merge",
                            "video_urls": [
                                "https://example.com/video1.mp4",
                                "https://example.com/video2.mp4"
                            ]
                        },
                        "response": "Returns download_url for merged video"
                    }
                }),
            }

        if method == "POST":
            try:
                body = event.get("body")
                if event.get("isBase64Encoded"):
                    body = base64.b64decode(body)
                if isinstance(body, (bytes, bytearray)):
                    body = body.decode("utf-8")
                data = json.loads(body or "{}")
            except Exception:
                data = event if isinstance(event, dict) else {}

            operation = data.get("operation", "remux")  # "remux" or "merge"
            
            if operation == "merge":
                return _handle_merge_operation(data)
            else:
                return _handle_remux_operation(data)

        # Handle GET /status/{job_id} for job status checking
        if method == "GET":
            path = event.get("rawPath", "")
            if path.startswith("/status/"):
                job_id = path.split("/status/")[-1]
                if job_id:
                    status_data = _get_job_status(job_id)
                    if status_data:
                        return {
                            "statusCode": 200,
                            "headers": {"content-type": "application/json"},
                            "body": json.dumps(status_data),
                        }
                    else:
                        return {
                            "statusCode": 404,
                            "headers": {"content-type": "application/json"},
                            "body": json.dumps({"error": "Job not found"}),
                        }

        return {
            "statusCode": 405,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": f"method {method} not allowed"}),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({
                "error": f"Handler error: {str(e)}",
                "event": str(event)[:500]  # Truncate for safety
            }),
        }
