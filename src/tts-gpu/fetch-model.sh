#!/usr/bin/env bash
# Pulls gwen-tts's built-in reference speakers (voice-clone conditioning
# audio + transcripts) from its public GitHub repo into gwen_data/ before
# `docker build` — same "fetch before build, bake into image" pattern as
# ../tts-generate/fetch-model.sh, just sourced from GitHub instead of GCS
# since these files aren't already staged in our bucket. The actual model
# weights (g-group-ai-lab/gwen-tts-0.6B, ~1.2GB) are fetched separately,
# straight from HuggingFace, inside the Dockerfile itself.
set -euo pipefail
cd "$(dirname "$0")"

rm -rf gwen_data .gwen-tts-src
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/ggroup-ai-lab/gwen-tts.git .gwen-tts-src
git -C .gwen-tts-src sparse-checkout set data

mkdir -p gwen_data
cp -r .gwen-tts-src/data/ref_audio gwen_data/
cp .gwen-tts-src/data/ref_info.json gwen_data/
rm -rf .gwen-tts-src
