import json
import os
import tempfile
import subprocess
from pathlib import Path
import base64

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


def handler(event, context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

    if method == "GET":
        return {
            "statusCode": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({
                "status": "ok",
                "message": "video-processing-api healthy",
                "has_ffmpeg": _has_ffmpeg() is not None,
                "mount_path_exists": Path(MOUNT_PATH).exists(),
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

    return {
        "statusCode": 405,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"error": f"method {method} not allowed"}),
    }
