#!/usr/bin/env bash
set -euo pipefail

API_ENDPOINT=${API_ENDPOINT:-$(cd "$(dirname "$0")/.." && terraform -chdir=video-processing-api output -raw api_endpoint 2>/dev/null || true)}

if [ -z "${API_ENDPOINT}" ]; then
  echo "API endpoint not provided. Set API_ENDPOINT env var or run from repo with Terraform outputs available." >&2
  exit 1
fi

if [ $# -lt 2 ]; then
  echo "Usage: $0 <video_url_1> <video_url_2> [more_urls...]" >&2
  exit 1
fi

# Build JSON array of URLs
urls_json=$(printf '"%s",' "$@" | sed 's/,$//')

resp=$(curl -s -X POST "${API_ENDPOINT}/process" \
  -H 'content-type: application/json' \
  -d "{\n  \"operation\": \"merge\",\n  \"video_urls\": [${urls_json}]\n}")

echo "Response: $resp"

job_id=$(echo "$resp" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("job_id",""))' 2>/dev/null || true)
status_url=$(echo "$resp" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status_url",""))' 2>/dev/null || true)

if [ -n "$job_id" ]; then
  echo "Job ID: $job_id"
fi
if [ -n "$status_url" ]; then
  echo "Status URL: $status_url"
fi
