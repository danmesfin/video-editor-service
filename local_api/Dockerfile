FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates ffmpeg && rm -rf /var/lib/apt/lists/*

# Python deps for local API and AWS SDK
RUN pip install --no-cache-dir fastapi uvicorn boto3

# App code will be mounted via volumes in docker-compose
EXPOSE 8000

CMD ["uvicorn", "local_api.app:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
