#!/usr/bin/env bash
set -euo pipefail

API_ENDPOINT=${API_ENDPOINT:-$(cd "$(dirname "$0")/.." && terraform -chdir=video-processing-api output -raw api_endpoint 2>/dev/null || true)}
JOB_ID=${1:-}
OUT_FILE=${2:-merged-output.mp4}

if [ -z "${API_ENDPOINT}" ] || [ -z "${JOB_ID}" ]; then
  echo "Usage: $0 <job_id> [output_file]  (and set API_ENDPOINT or run from repo root)" >&2
  exit 1
fi

RESP=$(curl -s "${API_ENDPOINT}/status/${JOB_ID}") || true
DOWNLOAD_URL=$(echo "$RESP" | python3 -c 'import sys,json; d=json.load(sys.stdin); print((d.get("metadata") or {}).get("download_url",""))' 2>/dev/null || true)

if [ -z "$DOWNLOAD_URL" ]; then
  echo "No download_url available in job status. Status may not be completed yet." >&2
  echo "$RESP"
  exit 2
fi

curl -L "$DOWNLOAD_URL" -o "$OUT_FILE"
echo "Saved to $OUT_FILE"
