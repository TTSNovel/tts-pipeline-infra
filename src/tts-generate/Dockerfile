FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Piper voice baked into the image (no runtime HuggingFace download) — same
# reasoning as the AWS Lambda image: fast, deterministic cold starts.
COPY piper_models/ piper_models/
COPY main.py .

# Cloud Run sets $PORT at runtime (defaults to 8080 locally). Single worker:
# the model is loaded once per worker and this is a CPU-bound ONNX inference
# workload, not worth duplicating the ~300MB-resident model across workers
# for this traffic level — threads give some request concurrency instead.
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 4 --timeout 0 main:app
