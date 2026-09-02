#!/usr/bin/env bash
# Downloads the Piper voice models from GCS into piper_models/ before
# `docker build` — keeps the ~63MB-each binaries out of git while still
# baking them into the image (no runtime network calls, same cold-start
# property the AWS deployment's lazy-load fix depends on).
set -euo pipefail
cd "$(dirname "$0")"

: "${TF_VAR_project_id:?TF_VAR_project_id not set — see .envrc}"
BUCKET="${TF_VAR_project_id}-tts-assets"

mkdir -p piper_models
for voice in vi_VN-vais1000-medium en_US-lessac-medium; do
  gcloud storage cp "gs://${BUCKET}/piper/${voice}.onnx" piper_models/
  gcloud storage cp "gs://${BUCKET}/piper/${voice}.onnx.json" piper_models/
done
