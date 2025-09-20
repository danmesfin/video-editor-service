#!/usr/bin/env python3
"""
End-to-end test runner for the Video Merge API.

Usage examples:

  # Use Terraform output to discover API endpoint, kick off a job with two URLs,
  # poll status until completion, and download result to merged-output.mp4
  python3 tests/e2e_test.py \
    --video-url "https://example.com/video1.mp4" \
    --video-url "https://example.com/video2.mp4" \
    --output merged-output.mp4

  # Or provide the API explicitly
  API_ENDPOINT=https://ehe5e2scsh.execute-api.eu-north-1.amazonaws.com \
  python3 tests/e2e_test.py \
    --video-url "https://example.com/video1.mp4" \
    --video-url "https://example.com/video2.mp4"

Environment variables:
  - API_ENDPOINT: Optional override for the API endpoint. If not set, the script
    tries `terraform -chdir=video-processing-api output -raw api_endpoint`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import subprocess
from typing import List, Optional


def get_api_endpoint() -> str:
    api = os.getenv("API_ENDPOINT", "").strip()
    if api:
        return api
    # Try to get from Terraform outputs
    try:
        result = subprocess.run(
            ["terraform", "-chdir=video-processing-api", "output", "-raw", "api_endpoint"],
            check=True,
            capture_output=True,
            text=True,
        )
        api = result.stdout.strip()
        if api:
            return api
    except Exception:
        pass
    raise SystemExit("API endpoint not provided. Set API_ENDPOINT or run from repo root with Terraform outputs available.")


def http_json(url: str, method: str = "GET", payload: Optional[dict] = None) -> dict:
    data = None
    headers = {"content-type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req) as resp:
        body = resp.read().decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"Non-JSON response from {url}: {body[:200]}")


def download(url: str, out_path: str) -> None:
    with urllib.request.urlopen(url) as resp, open(out_path, "wb") as f:
        f.write(resp.read())


def start_merge_job(api: str, video_urls: List[str]) -> dict:
    url = urllib.parse.urljoin(api + "/", "process")
    payload = {"operation": "merge", "video_urls": video_urls}
    return http_json(url, method="POST", payload=payload)


def get_status(api: str, job_id: str) -> dict:
    url = urllib.parse.urljoin(api + "/", f"status/{job_id}")
    return http_json(url)


def run_e2e(video_urls: List[str], out_path: str, timeout_sec: int = 900, poll_sec: int = 5) -> int:
    api = get_api_endpoint()
    print(f"API={api}")

    # 1) Start merge
    resp = start_merge_job(api, video_urls)
    print("Start response:", json.dumps(resp, indent=2))
    job_id = resp.get("job_id") or resp.get("JobId")
    if not job_id:
        print("Failed to receive job_id from API response", file=sys.stderr)
        return 1
    print(f"JOB_ID={job_id}")

    # 2) Poll until completed/failed or timeout
    deadline = time.time() + timeout_sec
    last_status = None
    while time.time() < deadline:
        try:
            status = get_status(api, job_id)
        except Exception as e:
            print(f"Status fetch error: {e}")
            time.sleep(poll_sec)
            continue
        last_status = status
        print(json.dumps(status, indent=2))
        st = status.get("status", "")
        if st == "completed":
            break
        if st == "failed":
            print("Job failed", file=sys.stderr)
            return 2
        time.sleep(poll_sec)

    if not last_status or last_status.get("status") != "completed":
        print("Timed out waiting for completion", file=sys.stderr)
        return 3

    # 3) Download result
    download_url = (last_status.get("metadata") or {}).get("download_url")
    if not download_url:
        print("No download_url present in completed status", file=sys.stderr)
        return 4
    print(f"Downloading to {out_path} ...")
    download(download_url, out_path)
    try:
        size = os.path.getsize(out_path)
        print(f"Saved {out_path} ({size} bytes)")
    except OSError:
        pass
    return 0


def parse_args(argv: List[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="End-to-end test for Video Merge API")
    p.add_argument("--video-url", dest="video_urls", action="append", required=True,
                   help="Video URL to include (specify at least twice)")
    p.add_argument("--output", dest="output", default="merged-output.mp4",
                   help="Where to save the merged output video")
    p.add_argument("--timeout", dest="timeout", type=int, default=900,
                   help="Timeout in seconds (default 900s)")
    p.add_argument("--poll", dest="poll", type=int, default=5,
                   help="Polling interval in seconds (default 5s)")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    if len(args.video_urls) < 2:
        print("Please provide at least two --video-url arguments", file=sys.stderr)
        sys.exit(1)
    sys.exit(run_e2e(args.video_urls, args.output, args.timeout, args.poll))
