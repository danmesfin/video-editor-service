#!/usr/bin/env bash
set -euo pipefail

API_ENDPOINT=${API_ENDPOINT:-$(cd "$(dirname "$0")/.." && terraform -chdir=video-processing-api output -raw api_endpoint 2>/dev/null || true)}
JOB_ID=${1:-}

if [ -z "${API_ENDPOINT}" ] || [ -z "${JOB_ID}" ]; then
  echo "Usage: $0 <job_id>  (and set API_ENDPOINT or run from repo root)" >&2
  exit 1
fi

while true; do
  RESP=$(curl -s "${API_ENDPOINT}/status/${JOB_ID}") || true
  echo "$(date +%H:%M:%S) -> $RESP"
  STATUS=$(echo "$RESP" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status",""))' 2>/dev/null || true)
  if [ "$STATUS" = "completed" ] || [ "$STATUS" = "failed" ] || [ -z "$STATUS" ]; then
    break
  fi
  sleep 5
done
