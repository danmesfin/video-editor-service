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

# Allow endpoint overrides for local testing (e.g., LocalStack)
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
SQS_ENDPOINT_URL = os.getenv("SQS_ENDPOINT_URL")

s3 = boto3.client("s3", endpoint_url=S3_ENDPOINT_URL) if S3_ENDPOINT_URL else boto3.client("s3")
try:
    sqs = boto3.client("sqs", endpoint_url=SQS_ENDPOINT_URL) if SQS_ENDPOINT_URL else boto3.client("sqs")
except Exception:
    sqs = None

INPUT_BUCKET = os.getenv("INPUT_BUCKET")
OUTPUT_BUCKET = os.getenv("OUTPUT_BUCKET")
MOUNT_PATH = os.getenv("MOUNT_PATH", "/mnt/efs")
QUEUE_URL = os.getenv("QUEUE_URL", "")


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


def _has_ffprobe() -> str | None:
    """Find ffprobe binary alongside ffmpeg layer if available."""
    candidates = [
        "/opt/bin/ffprobe",
        "/opt/ffprobe",
        "/usr/bin/ffprobe",
        "/usr/local/bin/ffprobe",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _input_has_audio(input_path: str, ffprobe_path: str | None) -> bool:
    """Return True if input file has at least one audio stream. If ffprobe is not available, assume True."""
    if not ffprobe_path:
        return True
    try:
        # If any audio stream exists, ffprobe will print an index
        proc = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", input_path],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out = (proc.stdout or b"").decode("utf-8").strip()
        return len(out) > 0
    except Exception:
        # Be permissive if detection fails
        return True


def _download_video_from_url(url: str, output_path: str) -> None:
    """Download video from URL to local path with robust handling.
    - If URL looks like S3, use boto3.
    - Otherwise, fetch via urllib with a browser-like User-Agent and stream to disk.
      On failure, fallback to `curl -L --fail --retry 3` if available.
    """
    # Ensure parent directory exists
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path.lstrip('/')
    # Virtual-hosted-style S3
    if host.endswith('.amazonaws.com') and '.s3.' in host:
        bucket = host.split('.s3.')[0]
        key = path
        s3.download_file(bucket, key, output_path)
        return
    # Path-style S3
    if host.startswith('s3.') and '/' in path:
        bucket, key = path.split('/', 1)
        s3.download_file(bucket, key, output_path)
        return

    # Generic HTTP(S)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
            "Accept": "*/*",
            "Connection": "keep-alive",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            # Stream to disk in chunks
            with open(output_path, 'wb') as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        return
    except Exception:
        # Fallback to curl if available (handles redirects and some TLS peculiarities)
        curl_bin = None
        for p in ["/usr/bin/curl", "/bin/curl", "/usr/local/bin/curl"]:
            if os.path.isfile(p) and os.access(p, os.X_OK):
                curl_bin = p
                break
        if curl_bin:
            try:
                subprocess.run(
                    [curl_bin, "-L", "--fail", "--retry", "3", "--connect-timeout", "20", "-sS", url, "-o", output_path],
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                return
            except Exception as e:
                raise RuntimeError(f"curl download failed: {e}")
        # If no curl or still failing, raise a descriptive error
        raise


def _generate_presigned_url(bucket: str, key: str, expiration: int = 3600) -> str:
    """Generate presigned URL for S3 object download"""
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': bucket, 'Key': key},
        ExpiresIn=expiration
    )


def _save_job_status(job_id: str, status: str, metadata: dict | None = None, progress: float | None = None):
    """Save job status to S3 for tracking. Optionally include progress 0-100."""
    status_data = {
        "job_id": job_id,
        "status": status,
        "timestamp": str(int(time.time())),
        "metadata": metadata or {}
    }
    if progress is not None:
        # Clamp and round for readability
        try:
            pct = max(0.0, min(100.0, float(progress)))
        except Exception:
            pct = None
        if pct is not None:
            status_data["progress"] = round(pct, 1)
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


def _handle_caption_operation(data: dict, worker_mode: bool = False):
    """Handle adding captions to video"""
    input_url = data.get("input", {}).get("url")
    caption_config = data.get("caption", {})
    
    if not input_url or not caption_config.get("text"):
        error_msg = "Caption operation requires input.url and caption.text"
        if worker_mode:
            raise ValueError(error_msg)
        return {"statusCode": 400, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": error_msg})}

    ffmpeg_path = _has_ffmpeg()
    if not ffmpeg_path:
        error_msg = "FFmpeg not available - caption operation requires FFmpeg layer"
        return {"statusCode": 400, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": error_msg})}
    
    try:
        job_id = data.get("job_id") or str(uuid.uuid4())[:8]
        _save_job_status(job_id, "processing", {"input_url": input_url, "caption": caption_config}, progress=10)
        
        tmp_dir = Path(MOUNT_PATH) / f"caption_{job_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        
        # Download input
        input_path = tmp_dir / "input.mp4"
        _download_video_from_url(input_url, str(input_path))
        _save_job_status(job_id, "processing", {"stage": "downloaded"}, progress=30)
        
        # Apply caption (preserve audio if present; inject silent track if missing)
        output_path = tmp_dir / "output.mp4"
        text = caption_config.get("text", "")
        font_size = caption_config.get("size", 24)
        color = caption_config.get("color", "white")
        position = caption_config.get("position", "bottom")

        # Map position to FFmpeg drawtext coordinates
        # New: support percentage-based x/y in caption.position = {"x": 0-100, "y": 0-100}
        pos_filter: str
        if isinstance(position, dict) and "x" in position and "y" in position:
            try:
                x_pct = max(0.0, min(100.0, float(position.get("x", 0)))) / 100.0
                y_pct = max(0.0, min(100.0, float(position.get("y", 0)))) / 100.0
                # Place text with top-left anchored proportionally, adjusted to stay fully in frame
                # Using (w-text_w) and (h-text_h) keeps text within bounds and roughly centers at the percentage
                pos_filter = f"x=(w-text_w)*{x_pct}:y=(h-text_h)*{y_pct}"
            except Exception:
                # Fallback to default if parsing fails
                pos_filter = "x=(w-text_w)/2:y=h-text_h-20"
        else:
            # Legacy named positions
            if position == "bottom":
                pos_filter = "x=(w-text_w)/2:y=h-text_h-20"
            elif position == "top":
                pos_filter = "x=(w-text_w)/2:y=20"
            elif position == "center":
                pos_filter = "x=(w-text_w)/2:y=(h-text_h)/2"
            else:
                pos_filter = "x=(w-text_w)/2:y=h-text_h-20"  # default bottom

        # Build filter and mapping to keep or add audio
        has_audio = _input_has_audio(str(input_path), _has_ffprobe())

        # Optional text stroke/outline support via FFmpeg drawtext border options
        stroke_cfg = caption_config.get("stroke") or caption_config.get("outline")
        stroke_width = 0
        stroke_color = "black"
        try:
            if isinstance(stroke_cfg, dict):
                stroke_width = int(stroke_cfg.get("width", 0) or 0)
                stroke_color = str(stroke_cfg.get("color", "black"))
            elif isinstance(stroke_cfg, (int, float)):
                stroke_width = int(stroke_cfg)
            elif stroke_cfg is True:
                stroke_width = 2
        except Exception:
            stroke_width = 0

        # Support true center alignment for multi-line text by drawing each line separately,
        # centering each line horizontally via (w-text_w)/2 and stacking vertically.
        def _escape_drawtext(s: str) -> str:
            # Escape characters significant to drawtext option parsing
            return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

        lines = str(text).split("\n") if isinstance(text, str) else [str(text)]
        num_lines = len(lines)
        line_spacing = int(caption_config.get("line_spacing", 10))
        line_height = int(font_size) + line_spacing
        total_height = num_lines * line_height

        # Horizontal position expression per line
        # Default: center horizontally; if percentage provided, honor it
        # Named positions -> center horizontally for better readability
        x_expr = "(w-text_w)/2"
        y0_expr = "(h-{} )/2".format(total_height)

        # If percentage object provided, place the whole block top at y0 based on total height
        if isinstance(position, dict) and "x" in position and "y" in position:
            try:
                x_pct = max(0.0, min(100.0, float(position.get("x", 0)))) / 100.0
                y_pct = max(0.0, min(100.0, float(position.get("y", 0)))) / 100.0
                x_expr = f"(w-text_w)*{x_pct}"
                y0_expr = f"(h-{total_height})*{y_pct}"
            except Exception:
                pass
        else:
            # Legacy named positions
            if position == "top":
                y0_expr = "20"
            elif position == "bottom":
                y0_expr = f"h-{total_height}-20"
            elif position == "center":
                y0_expr = f"(h-{total_height})/2"

        # Build chained drawtext filters so each line is truly centered
        filters: list[str] = []
        in_label = "0:v"
        out_label = "v0"
        for i, line in enumerate(lines):
            y_expr = f"{y0_expr}+{i}*{line_height}"
            draw = f"drawtext=text='{_escape_drawtext(line)}':fontsize={font_size}:fontcolor={color}"
            if stroke_width > 0:
                draw += f":bordercolor={stroke_color}:borderw={stroke_width}"
            draw += f":x={x_expr}:y={y_expr}"
            filters.append(f"[{in_label}]{draw}[{out_label}]")
            in_label = out_label
            out_label = f"v{i+1}"

        filter_complex = ";".join(filters)

        if has_audio:
            # Keep original audio, encode video
            cmd = [
                ffmpeg_path, "-y",
                "-i", str(input_path),
                "-filter_complex", filter_complex,
                "-map", f"[{in_label}]", "-map", "0:a",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy",
                str(output_path)
            ]
        else:
            # Inject a silent stereo audio track so output has audio
            cmd = [
                ffmpeg_path, "-y",
                "-i", str(input_path),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-shortest",
                "-filter_complex", filter_complex,
                "-map", f"[{in_label}]", "-map", "1:a",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                str(output_path)
            ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        _save_job_status(job_id, "uploading", {}, progress=80)
        
        # Upload result
        output_key = f"caption/{job_id}/output.mp4"
        s3.upload_file(str(output_path), OUTPUT_BUCKET, output_key)
        download_url = _generate_presigned_url(OUTPUT_BUCKET, output_key, 3600)
        
        _save_job_status(job_id, "completed", {"download_url": download_url}, progress=100)
        
        if worker_mode:
            return {"success": True, "job_id": job_id, "download_url": download_url}
        else:
            return {
                "statusCode": 200,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"success": True, "job_id": job_id, "download_url": download_url})
            }
            
    except Exception as e:
        jid = locals().get("job_id", data.get("job_id", "unknown"))
        _save_job_status(jid, "failed", {"error": str(e)}, progress=100)
        if worker_mode:
            raise
        return {"statusCode": 500, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": f"Caption operation failed: {str(e)}"})}


def _handle_audio_operation(data: dict, worker_mode: bool = False):
    """Handle adding audio overlay to video"""
    input_url = data.get("input", {}).get("url")
    audio_config = data.get("audio", {})
    audio_url = audio_config.get("url")
    
    if not input_url or not audio_url:
        error_msg = "Audio operation requires input.url and audio.url"
        if worker_mode:
            raise ValueError(error_msg)
        return {"statusCode": 400, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": error_msg})}

    ffmpeg_path = _has_ffmpeg()
    if not ffmpeg_path:
        error_msg = "FFmpeg not available - audio operation requires FFmpeg layer"
        return {"statusCode": 400, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": error_msg})}
    
    try:
        job_id = data.get("job_id") or str(uuid.uuid4())[:8]
        _save_job_status(job_id, "processing", {"input_url": input_url, "audio": audio_config}, progress=10)
        
        tmp_dir = Path(MOUNT_PATH) / f"audio_{job_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        
        # Download input video and audio
        input_path = tmp_dir / "input.mp4"
        audio_path = tmp_dir / "audio.mp3"
        _download_video_from_url(input_url, str(input_path))
        _download_video_from_url(audio_url, str(audio_path))
        _save_job_status(job_id, "processing", {"stage": "downloaded"}, progress=40)
        
        # Apply audio overlay
        output_path = tmp_dir / "output.mp4"
        volume = audio_config.get("volume", 1.0)
        start_time = float(audio_config.get("start", 0))
        start_ms = max(0, int(start_time * 1000))

        # Determine if input has audio; if not, we'll inject a silent stereo track
        has_audio = _input_has_audio(str(input_path), _has_ffprobe())

        if has_audio:
            # Inputs: 0 = video(with audio), 1 = overlay audio
            # Delay overlay audio by start_ms, scale volume, then amix
            filter_complex = f"[1:a]volume={volume},adelay={start_ms}|{start_ms}[bg];[0:a][bg]amix=inputs=2:duration=first:dropout_transition=3[aout]"
            cmd = [
                ffmpeg_path, "-y",
                "-i", str(input_path),
                "-i", str(audio_path),
                "-filter_complex", filter_complex,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-shortest",
                str(output_path)
            ]
        else:
            # Inject a silent stereo track as base audio
            # Inputs: 0 = video(no audio), 1 = overlay audio, 2 = generated silence
            filter_complex = f"[1:a]volume={volume},adelay={start_ms}|{start_ms}[bg];[2:a][bg]amix=inputs=2:duration=first:dropout_transition=3[aout]"
            cmd = [
                ffmpeg_path, "-y",
                "-i", str(input_path),
                "-i", str(audio_path),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-filter_complex", filter_complex,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                "-shortest",
                str(output_path)
            ]

        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        _save_job_status(job_id, "uploading", {}, progress=80)
        
        # Upload result
        output_key = f"audio/{job_id}/output.mp4"
        s3.upload_file(str(output_path), OUTPUT_BUCKET, output_key)
        download_url = _generate_presigned_url(OUTPUT_BUCKET, output_key, 3600)
        
        _save_job_status(job_id, "completed", {"download_url": download_url}, progress=100)
        
        if worker_mode:
            return {"success": True, "job_id": job_id, "download_url": download_url}
        else:
            return {
                "statusCode": 200,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"success": True, "job_id": job_id, "download_url": download_url})
            }
            
    except Exception as e:
        jid = locals().get("job_id", data.get("job_id", "unknown"))
        _save_job_status(jid, "failed", {"error": str(e)}, progress=100)
        if worker_mode:
            raise
        return {"statusCode": 500, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": f"Audio operation failed: {str(e)}"})}


def _handle_watermark_operation(data: dict, worker_mode: bool = False):
    """Handle adding watermark to video"""
    input_url = data.get("input", {}).get("url")
    watermark_config = data.get("watermark", {})
    watermark_url = watermark_config.get("url")
    
    if not input_url or not watermark_url:
        error_msg = "Watermark operation requires input.url and watermark.url"
        if worker_mode:
            raise ValueError(error_msg)
        return {"statusCode": 400, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": error_msg})}

    ffmpeg_path = _has_ffmpeg()
    if not ffmpeg_path:
        error_msg = "FFmpeg not available - watermark operation requires FFmpeg layer"
        return {"statusCode": 400, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": error_msg})}
    
    try:
        job_id = data.get("job_id") or str(uuid.uuid4())[:8]
        _save_job_status(job_id, "processing", {"input_url": input_url, "watermark": watermark_config}, progress=10)
        
        tmp_dir = Path(MOUNT_PATH) / f"watermark_{job_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        
        # Download input video and watermark
        input_path = tmp_dir / "input.mp4"
        watermark_path = tmp_dir / "watermark.png"
        _download_video_from_url(input_url, str(input_path))
        _download_video_from_url(watermark_url, str(watermark_path))
        _save_job_status(job_id, "processing", {"stage": "downloaded"}, progress=40)
        
        # Apply watermark
        output_path = tmp_dir / "output.mp4"
        position = watermark_config.get("position", "top-right")
        opacity = watermark_config.get("opacity", 1.0)
        scale = watermark_config.get("scale", 0.1)
        
        # Map position to FFmpeg overlay coordinates
        # Support either named anchors or percentage-based x/y dict
        if isinstance(position, dict) and "x" in position and "y" in position:
            try:
                x_pct = max(0.0, min(100.0, float(position.get("x", 0)))) / 100.0
                y_pct = max(0.0, min(100.0, float(position.get("y", 0)))) / 100.0
                # (W-w) and (H-h) keep the watermark fully within the frame
                pos_filter = f"(W-w)*{x_pct}:(H-h)*{y_pct}"
            except Exception:
                # Fallback to default if parsing fails
                pos_filter = "W-w-10:10"
        else:
            if position == "top-left":
                pos_filter = "10:10"
            elif position == "top-right":
                pos_filter = "W-w-10:10"
            elif position == "bottom-left":
                pos_filter = "10:H-h-10"
            elif position == "bottom-right":
                pos_filter = "W-w-10:H-h-10"
            elif position == "center":
                pos_filter = "(W-w)/2:(H-h)/2"
            else:
                pos_filter = "W-w-10:10"  # default top-right
        
        subprocess.run([
            ffmpeg_path, "-y", "-i", str(input_path), "-i", str(watermark_path),
            "-filter_complex", f"[1:v]scale=iw*{scale}:ih*{scale},format=rgba,colorchannelmixer=aa={opacity}[wm];[0:v][wm]overlay={pos_filter}",
            "-c:a", "copy", str(output_path)
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        _save_job_status(job_id, "uploading", {}, progress=80)
        
        # Upload result
        output_key = f"watermark/{job_id}/output.mp4"
        s3.upload_file(str(output_path), OUTPUT_BUCKET, output_key)
        download_url = _generate_presigned_url(OUTPUT_BUCKET, output_key, 3600)
        
        _save_job_status(job_id, "completed", {"download_url": download_url}, progress=100)
        
        if worker_mode:
            return {"success": True, "job_id": job_id, "download_url": download_url}
        else:
            return {
                "statusCode": 200,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"success": True, "job_id": job_id, "download_url": download_url})
            }
            
    except Exception as e:
        jid = locals().get("job_id", data.get("job_id", "unknown"))
        _save_job_status(jid, "failed", {"error": str(e)}, progress=100)
        if worker_mode:
            raise
        return {"statusCode": 500, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": f"Watermark operation failed: {str(e)}"})}


def _handle_overlay_operation(data: dict, worker_mode: bool = False):
    """Handle adding video overlay"""
    input_url = data.get("input", {}).get("url")
    overlay_config = data.get("overlay", {})
    overlay_url = overlay_config.get("url")
    
    if not input_url or not overlay_url:
        error_msg = "Overlay operation requires input.url and overlay.url"
        if worker_mode:
            raise ValueError(error_msg)
        return {"statusCode": 400, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": error_msg})}

    ffmpeg_path = _has_ffmpeg()
    if not ffmpeg_path:
        error_msg = "FFmpeg not available - overlay operation requires FFmpeg layer"
        return {"statusCode": 400, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": error_msg})}
    
    try:
        job_id = data.get("job_id") or str(uuid.uuid4())[:8]
        _save_job_status(job_id, "processing", {"input_url": input_url, "overlay": overlay_config}, progress=10)
        
        tmp_dir = Path(MOUNT_PATH) / f"overlay_{job_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        
        # Download input video and overlay
        input_path = tmp_dir / "input.mp4"
        overlay_path = tmp_dir / "overlay.mp4"
        _download_video_from_url(input_url, str(input_path))
        _download_video_from_url(overlay_url, str(overlay_path))
        _save_job_status(job_id, "processing", {"stage": "downloaded"}, progress=40)
        
        # Apply overlay
        output_path = tmp_dir / "output.mp4"
        position = overlay_config.get("position", {})
        # Position may be pixel-based or percentage-based {x:0-100,y:0-100}
        x_expr = None
        y_expr = None
        if isinstance(position, dict) and "x" in position and "y" in position:
            try:
                x_val = float(position.get("x", 10))
                y_val = float(position.get("y", 10))
                if 0.0 <= x_val <= 100.0 and 0.0 <= y_val <= 100.0:
                    # Treat as percentage
                    x_expr = f"(W-w)*{x_val/100.0}"
                    y_expr = f"(H-h)*{y_val/100.0}"
                else:
                    # Treat as absolute pixels
                    x_expr = str(int(x_val))
                    y_expr = str(int(y_val))
            except Exception:
                x_expr = "10"
                y_expr = "10"
        else:
            x_expr = "10"
            y_expr = "10"
        size = overlay_config.get("size", {})
        width = size.get("width", 320)
        height = size.get("height", 240)
        start_time = overlay_config.get("start", 0)
        duration = overlay_config.get("duration", 10)
        
        subprocess.run([
            ffmpeg_path, "-y", "-i", str(input_path), "-i", str(overlay_path),
            "-filter_complex", f"[1:v]scale={width}:{height}[ov];[0:v][ov]overlay={x_expr}:{y_expr}:enable='between(t,{start_time},{start_time + duration})'",
            "-c:a", "copy", str(output_path)
        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        _save_job_status(job_id, "uploading", {}, progress=80)
        
        # Upload result
        output_key = f"overlay/{job_id}/output.mp4"
        s3.upload_file(str(output_path), OUTPUT_BUCKET, output_key)
        download_url = _generate_presigned_url(OUTPUT_BUCKET, output_key, 3600)
        
        _save_job_status(job_id, "completed", {"download_url": download_url}, progress=100)
        
        if worker_mode:
            return {"success": True, "job_id": job_id, "download_url": download_url}
        else:
            return {
                "statusCode": 200,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"success": True, "job_id": job_id, "download_url": download_url})
            }
            
    except Exception as e:
        jid = locals().get("job_id", data.get("job_id", "unknown"))
        _save_job_status(jid, "failed", {"error": str(e)}, progress=100)
        if worker_mode:
            raise
        return {"statusCode": 500, "headers": {"content-type": "application/json"}, "body": json.dumps({"error": f"Overlay operation failed: {str(e)}"})}


def _handle_merge_operation(data: dict, worker_mode: bool = False):
    """Handle video merge operation from URLs"""
    video_urls = data.get("video_urls", [])
    
    if len(video_urls) < 2:
        if worker_mode:
            raise ValueError("Merge operation requires at least 2 video URLs")
        return {
            "statusCode": 400,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": "Merge operation requires at least 2 video URLs"}),
        }

    ffmpeg_path = _has_ffmpeg()
    if not ffmpeg_path:
        return {
            "statusCode": 400,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": "FFmpeg not available - merge operation requires FFmpeg layer"}),
            "body": json.dumps({
                "error": "FFmpeg not available - merge operation requires FFmpeg layer",
            }),
        }
    
    try:
        ffprobe_path = _has_ffprobe()
        # Use existing job_id if present (SQS), else generate new
        job_id = data.get("job_id") or str(uuid.uuid4())[:8]
        
        # Save initial job status
        _save_job_status(job_id, "processing", {
            "video_urls": video_urls,
            "videos_count": len(video_urls),
            "started_at": time.time()
        }, progress=5)
        
        tmp_dir = Path(MOUNT_PATH) / f"merge_{job_id}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        
        # Update status: downloading
        _save_job_status(job_id, "downloading", {
            "video_urls": video_urls,
            "videos_count": len(video_urls)
        }, progress=10)
        
        # Download all videos from URLs
        input_paths = []
        for i, url in enumerate(video_urls):
            # Get file extension from URL or default to .mp4
            parsed_url = urlparse(url)
            ext = Path(parsed_url.path).suffix or '.mp4'
            input_path = tmp_dir / f"input_{i}{ext}"
            
            _download_video_from_url(url, str(input_path))
            input_paths.append(str(input_path))
            # Update incremental download progress: 10% -> 40%
            dl_prog = 10 + (30 * (i + 1) / len(video_urls))
            _save_job_status(job_id, "downloading", {
                "video_urls": video_urls,
                "videos_count": len(video_urls),
                "downloaded": i + 1
            }, progress=dl_prog)
        
        # Update status: merging
        _save_job_status(job_id, "merging", {
            "video_urls": video_urls,
            "videos_count": len(video_urls)
        }, progress=45)
        
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
            # If the input has no audio, add a silent stereo track and use -shortest to match video duration.
            has_audio = _input_has_audio(str(input_path), ffprobe_path)
            if has_audio:
                cmd = [
                    ffmpeg_path, "-y", "-i", str(input_path),
                    "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                    "-r", "30", "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(normalized_path)
                ]
            else:
                # input has no audio -> add silent audio via anullsrc
                cmd = [
                    ffmpeg_path, "-y",
                    "-i", str(input_path),
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-shortest",
                    "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
                    "-r", "30", "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    str(normalized_path)
                ]
            subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            normalized_paths.append(str(normalized_path))
            # Update normalization progress: 45% -> 95%
            norm_prog = 45 + (50 * (i + 1) / len(input_paths))
            _save_job_status(job_id, "merging", {
                "video_urls": video_urls,
                "videos_count": len(video_urls),
                "normalized": i + 1
            }, progress=norm_prog)
        
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
        _save_job_status(job_id, "merging", {"stage": "concat"}, progress=98)
        
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
        }, progress=100)
        if worker_mode:
            return {"success": True, "job_id": job_id, "videos_merged": len(video_urls), "download_url": download_url}
        else:
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
            # job_id may not exist if failure before assignment
            jid = locals().get("job_id", data.get("job_id", "unknown"))
            _save_job_status(jid, "failed", {"error": str(e), "failed_at": time.time()}, progress=100)
        except Exception:
            pass
        if worker_mode:
            # Reraise to make SQS retry
            raise
        return {
            "statusCode": 500,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"error": f"Merge operation failed: {str(e)}", "operation": "merge"}),
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
        # SQS trigger
        if isinstance(event, dict) and isinstance(event.get("Records"), list):
            for record in event["Records"]:
                if record.get("eventSource") == "aws:sqs":
                    payload = json.loads(record.get("body", "{}"))
                    operation = payload.get("operation", "merge")
                    if operation == "merge":
                        _handle_merge_operation(payload, worker_mode=True)
                    elif operation == "caption":
                        _handle_caption_operation(payload, worker_mode=True)
                    elif operation == "add-audio":
                        _handle_audio_operation(payload, worker_mode=True)
                    elif operation == "watermark":
                        _handle_watermark_operation(payload, worker_mode=True)
                    elif operation == "overlay":
                        _handle_overlay_operation(payload, worker_mode=True)
                    else:
                        raise ValueError(f"Unknown operation: {operation}")
            # Successful processing
            return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": json.dumps({"status": "ok"})}

        method = event.get("requestContext", {}).get("http", {}).get("method", "GET")

        if method == "GET":
            # Route GET /status/{job_id} first
            raw_path = event.get("rawPath", "") or event.get("path", "")
            if isinstance(raw_path, str) and raw_path.startswith("/status/"):
                job_id = raw_path.split("/status/")[-1]
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
            # Otherwise return health root
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

            operation = data.get("operation", "remux")
            
            # Handle all async operations
            if operation in ["merge", "caption", "add-audio", "watermark", "overlay"]:
                # Async enqueue if queue configured
                if QUEUE_URL and sqs is not None:
                    job_id = data.get("job_id") or str(uuid.uuid4())[:8]
                    data["job_id"] = job_id
                    
                    # Save initial status based on operation
                    if operation == "merge":
                        _save_job_status(job_id, "queued", {"video_urls": data.get("video_urls", []), "enqueued_at": time.time()})
                    else:
                        _save_job_status(job_id, "queued", {"input_url": data.get("input", {}).get("url"), "operation": operation, "enqueued_at": time.time()})
                    
                    sqs.send_message(QueueUrl=QUEUE_URL, MessageBody=json.dumps(data))
                    # Build status URL for convenience
                    domain = event.get("requestContext", {}).get("domainName", "")
                    status_url = f"https://{domain}/status/{job_id}" if domain else f"/status/{job_id}"
                    return {
                        "statusCode": 202,
                        "headers": {"content-type": "application/json"},
                        "body": json.dumps({"accepted": True, "job_id": job_id, "status_url": status_url})
                    }
                
                # Fallback to sync processing
                if operation == "merge":
                    return _handle_merge_operation(data)
                elif operation == "caption":
                    return _handle_caption_operation(data)
                elif operation == "add-audio":
                    return _handle_audio_operation(data)
                elif operation == "watermark":
                    return _handle_watermark_operation(data)
                elif operation == "overlay":
                    return _handle_overlay_operation(data)
            else:
                # Legacy remux operation
                return _handle_remux_operation(data)

        # (status GET handled above before health response)

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
