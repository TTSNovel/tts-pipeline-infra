# nvidia/cuda *devel* (not the runtime-only python:3.11-slim used before) —
# flash-attn's setup.py compiles CUDA kernels via nvcc at install time,
# which needs the full toolkit + headers under a working CUDA_HOME. The
# devel image ships that pre-wired at /usr/local/cuda; cobbling the same
# thing together from individual nvidia-*-cu12 pip packages on a slim
# base is a known source of misconfigured-CUDA_HOME build failures.
FROM nvidia/cuda:12.4.1-devel-ubuntu22.04

WORKDIR /app

# tzdata (pulled in transitively by python3.11-dev below) prompts
# interactively for a timezone with no TTY to answer it on Cloud Build —
# hangs the build indefinitely otherwise (confirmed: first attempt sat at
# this prompt until cancelled).
ENV DEBIAN_FRONTEND=noninteractive TZ=Etc/UTC

# Ubuntu 22.04 ships python3.10 by default; qwen-tts requires >=3.11 —
# deadsnakes PPA for a plain python3.11 + venv + dev headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
      software-properties-common curl sox \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
      python3.11 python3.11-venv python3.11-dev \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sS https://bootstrap.pypa.io/get-pip.py | python3.11

RUN python3.11 -m pip install --no-cache-dir torch==2.5.1 torchaudio==2.5.1 \
    --index-url https://download.pytorch.org/whl/cu124

# flash-attn compiled from source against the toolkit above — no prebuilt
# wheel pinned here since availability is tied to the exact
# torch/cuda/python/cxx11abi combination, which shifts over time; building
# is slower (this is the reason the CUDA devel base + longer Cloud Build
# timeout/bigger machine are needed) but always matches what's installed.
# MAX_JOBS bounds parallel nvcc jobs — flash-attn's build is memory-hungry
# per job, uncapped parallelism (one job per core) risks OOM-killing the
# build on a high-core-count machine.
RUN python3.11 -m pip install --no-cache-dir ninja packaging setuptools wheel
RUN MAX_JOBS=16 python3.11 -m pip install --no-cache-dir flash-attn==2.7.4.post1 --no-build-isolation

# --ignore-installed blinker: the deadsnakes python3.11 install picks up a
# distutils-installed system blinker 1.4 that pip can't cleanly uninstall
# to satisfy flask/flask-cors's newer-blinker dependency (confirmed by a
# failed build) — ignoring it just shadows the old one instead of trying
# (and failing) to remove it first.
RUN python3.11 -m pip install --no-cache-dir --ignore-installed blinker \
    qwen-tts soundfile numpy flask flask-cors gunicorn huggingface_hub

# qwen-tts's own dependency resolution silently pulled a plain-PyPI
# torch==2.13.0 (+ its own CUDA 13 runtime libs) over the pinned
# torch==2.5.1+cu124 above, since it doesn't constrain torch tightly —
# confirmed by a deploy where flash-attn (compiled against 2.5.1's C++
# ABI) silently failed to import at runtime and fell back to sdpa with no
# build-time error. --no-deps forces torch back down without touching
# qwen-tts's other already-installed dependencies (transformers etc.),
# and the import check right after fails the BUILD loudly if this ever
# regresses again, instead of only surfacing as a silent runtime fallback.
RUN python3.11 -m pip install --no-cache-dir --force-reinstall --no-deps \
    torch==2.5.1 torchaudio==2.5.1 --index-url https://download.pytorch.org/whl/cu124
RUN python3.11 -c "import flash_attn; print('flash_attn', flash_attn.__version__, 'import OK')"

# gwen-tts-0.6B weights (~1.2GB) pre-warmed into the image at build time —
# same "avoid a multi-GB runtime download" intent as tts-generate's Piper/
# vieneu models, but NOT fully offline: unlike those, qwen_tts's
# from_pretrained makes its own live Hub API call on every load (not just
# a local cache lookup) regardless of what's already on disk, so
# HF_HUB_OFFLINE=1 makes it error out instead of degrading gracefully —
# confirmed by a failed deploy. Leaving Hub API reachable costs one fast
# metadata round-trip per cold start, not a re-download, since the actual
# weight files are already warmed into HF_HOME below.
ENV HF_HOME=/app/hf_cache
RUN python3.11 -c "from huggingface_hub import snapshot_download; snapshot_download('g-group-ai-lab/gwen-tts-0.6B')"

# Built-in reference speakers, fetched by fetch-model.sh before this build
# (not part of the HF weights repo above, and not worth committing to git).
COPY gwen_data/ gwen_data/
COPY main.py .

# Cloud Run sets $PORT at runtime. Single worker/thread: one GPU per
# instance and max_instance_request_concurrency=1 (see Terraform) already
# limit each instance to one request at a time.
CMD exec python3.11 -m gunicorn --bind :${PORT:-8080} --workers 1 --threads 1 --timeout 0 main:app
