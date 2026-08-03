FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# VieNeu-TTS model weights (~200MB, pulled from HuggingFace on first
# instantiation) baked into the image at build time — HF_HOME pins the
# download into this layer instead of a container-ephemeral cache path, and
# HF_HUB_OFFLINE then forbids any runtime network fetch, so a cold start
# fails loud on an incomplete bake instead of silently hitting the network
# in prod. Same "zero runtime network calls" property as the Piper voices.
ENV HF_HOME=/app/hf_cache
RUN python3 -c "import vieneu; vieneu.Vieneu(mode='v3turbo')"
ENV HF_HUB_OFFLINE=1

# Piper voices baked into the image (no runtime HuggingFace download) — same
# reasoning as the AWS Lambda image: fast, deterministic cold starts.
COPY piper_models/ piper_models/
COPY vi_syllables.txt .
COPY lang_segment.py .
COPY transliterate.py .
COPY main.py .

# Cloud Run sets $PORT at runtime (defaults to 8080 locally). Single worker:
# the model is loaded once per worker and this is a CPU-bound ONNX inference
# workload, not worth duplicating the ~300MB-resident model across workers
# for this traffic level — threads give some request concurrency instead.
CMD exec gunicorn --bind :${PORT:-8080} --workers 1 --threads 4 --timeout 0 main:app
