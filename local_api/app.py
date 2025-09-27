import json
import os
import sys
import importlib.util
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Ensure we can import the lambda handler from source
LAMBDA_PATH = "/app/video-processing-api/lambda/main.py"
spec = importlib.util.spec_from_file_location("lambda_main", LAMBDA_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules["lambda_main"] = module
assert spec and spec.loader
spec.loader.exec_module(module)  # type: ignore
handler = module.handler

app = FastAPI(title="Local Video Processing API")

# Discover QUEUE_URL from SQS if not provided
try:
    import boto3 as _b
    sqs_ep = os.getenv("SQS_ENDPOINT_URL") or "http://localstack:4566"
    sqs_client = _b.client("sqs", endpoint_url=sqs_ep)
    # Ensure the lambda module uses the same SQS client (helps in local)
    setattr(module, "sqs", sqs_client)

    qname = os.getenv("QUEUE_NAME")
    env_qurl = os.getenv("QUEUE_URL", "")
    qurl = env_qurl
    if not qurl and qname:
        qurl = sqs_client.get_queue_url(QueueName=qname)["QueueUrl"]
    # Rewrite host to localstack for in-container access
    if qurl:
        qurl = qurl.replace("localhost:4566", "localstack:4566")
        qurl = qurl.replace("127.0.0.1:4566", "localstack:4566")
        setattr(module, "QUEUE_URL", qurl)
except Exception:
    pass


def _event_for_root_get() -> dict:
    return {
        "requestContext": {"http": {"method": "GET"}},
        "rawPath": "/",
    }


def _event_for_status_get(job_id: str) -> dict:
    return {
        "requestContext": {"http": {"method": "GET"}},
        "rawPath": f"/status/{job_id}",
    }


def _event_for_post(body: dict) -> dict:
    return {
        "requestContext": {
            "http": {"method": "POST"},
            "domainName": os.getenv("PUBLIC_DOMAIN", "localhost:8000"),
        },
        "isBase64Encoded": False,
        "body": json.dumps(body),
    }


@app.get("/")
async def root():
    resp = handler(_event_for_root_get(), None)
    if isinstance(resp, dict) and "statusCode" in resp:
        return JSONResponse(status_code=resp.get("statusCode", 200), content=json.loads(resp.get("body", "{}")))
    return resp


@app.get("/status/{job_id}")
async def status(job_id: str):
    resp = handler(_event_for_status_get(job_id), None)
    if isinstance(resp, dict) and "statusCode" in resp:
        content = resp.get("body")
        try:
            data = json.loads(content) if isinstance(content, str) else content
        except Exception:
            data = content
        return JSONResponse(status_code=resp.get("statusCode", 200), content=data)
    return resp


@app.post("/process")
async def process(request: Request):
    body = await request.json()
    resp = handler(_event_for_post(body), None)
    if isinstance(resp, dict) and "statusCode" in resp:
        content = resp.get("body")
        try:
            data = json.loads(content) if isinstance(content, str) else content
        except Exception:
            data = content
        # Convenience: if a presigned URL points to localstack host, provide localhost variant too
        try:
            if isinstance(data, dict):
                if "download_url" in data:
                    dl = data["download_url"]
                    data["download_url_local"] = dl.replace("localstack:4566", "localhost:4566")
                elif isinstance(data.get("metadata"), dict) and "download_url" in data["metadata"]:
                    dl = data["metadata"]["download_url"]
                    data["metadata"]["download_url_local"] = dl.replace("localstack:4566", "localhost:4566")
        except Exception:
            pass
        return JSONResponse(status_code=resp.get("statusCode", 200), content=data)
    return resp
