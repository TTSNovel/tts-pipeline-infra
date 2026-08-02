#!/usr/bin/env bash
# Downloads the Piper voice model from GCS into piper_models/ before
# `docker build` — keeps the ~63MB binary out of git while still baking it
# into the image (no runtime network calls, same cold-start property the
# AWS deployment's lazy-load fix depends on).
set -euo pipefail
cd "$(dirname "$0")"

BUCKET="${TF_VAR_project_id:-tts-pipeline-yl}-tts-assets"

mkdir -p piper_models
gcloud storage cp "gs://${BUCKET}/piper/vi_VN-vais1000-medium.onnx" piper_models/
gcloud storage cp "gs://${BUCKET}/piper/vi_VN-vais1000-medium.onnx.json" piper_models/
