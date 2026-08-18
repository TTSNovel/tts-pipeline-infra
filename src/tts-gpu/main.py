"""tts-gpu — gwen-tts (Qwen3-TTS-0.6B finetuned for Vietnamese voice
cloning), GPU-backed Cloud Run service, synchronous request/response.

Separate service from tts-generate (CPU-only) rather than a new MODELS
entry there: attaching a GPU to a Cloud Run service forces GPU billing/
sizing requirements (min 4 CPU / 16Gi mem, instance-based billing, see
Terraform) onto the WHOLE service, which would make every cheap
piper_vi/google_tts request pay GPU-instance pricing if they shared one.

Only one model family here, so no MODELS-registry dispatch like
tts-generate's — the request instead picks a built-in reference SPEAKER
(voice-clone conditioning audio bundled at build time, see
fetch-model.sh/gwen_data/ref_info.json), since gwen-tts always operates
via generate_voice_clone (no non-cloning "just talk" mode exists).

Uses flash-attn when available (falls back to sdpa otherwise, same as
gwen-tts's own inference.py) — measured ~2-3x real-time generation on
sdpa alone, flash-attn's faster attention noticeably helps the
autoregressive decode loop that dominates synthesis time.

Auth: same x-tts-secret / TTS_API_SECRET contract as tts-generate.
"""
import hmac
import io
import json
import os
import time

import numpy as np
import soundfile as sf
import torch
from flask import Flask, jsonify, request
from flask_cors import CORS

TTS_API_SECRET = os.environ["TTS_API_SECRET"]
MODEL_PATH = os.environ.get("GWEN_TTS_MODEL_PATH", "g-group-ai-lab/gwen-tts-0.6B")
DEFAULT_SPEAKER = os.environ.get("GWEN_TTS_DEFAULT_SPEAKER", "yen_nhi")

# 4096 max_new_tokens (GENERATION_CONFIG below) caps how much audio one
# call can produce; this bounds the input side so a too-long chunk fails
# fast with a clear error instead of silently truncating mid-sentence.
MAX_TEXT_CHARS = 1000

_DATA_DIR = os.path.join(os.path.dirname(__file__), "gwen_data")
with open(os.path.join(_DATA_DIR, "ref_info.json"), encoding="utf-8") as f:
    REF_INFO = json.load(f)

# gwen-tts README's recommended generation config, copied verbatim —
# these were tuned by the model authors for this specific finetune, not
# generic Qwen3-TTS defaults.
GENERATION_CONFIG = dict(
    temperature=0.3,
    top_k=20,
    top_p=0.9,
    max_new_tokens=4096,
    repetition_penalty=2.0,
    subtalker_do_sample=True,
    subtalker_temperature=0.1,
    subtalker_top_k=20,
    subtalker_top_p=1.0,
)

app = Flask(__name__)
CORS(app, resources={r"/tts/*": {"origins": "*"}}, allow_headers=["Content-Type", "x-tts-secret"])

# Lazy-loaded on first request, not at import time — keeps cold start off
# any container-startup-phase timeout, same reasoning as tts-generate's
# get_voice()/_get_vieneu().
_model = None


def get_model():
    global _model
    if _model is None:
        from qwen_tts import Qwen3TTSModel

        try:
            import flash_attn  # noqa: F401
            attn_impl = "flash_attention_2"
        except Exception as e:
            attn_impl = "sdpa"
            print(f"tts-gpu: flash-attn import failed ({type(e).__name__}: {e}), falling back to sdpa", flush=True)

        t0 = time.perf_counter()
        _model = Qwen3TTSModel.from_pretrained(
            MODEL_PATH,
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation=attn_impl,
        )
        print(f"tts-gpu: model load {(time.perf_counter() - t0) * 1000:.1f}ms attn={attn_impl}", flush=True)
    return _model


def synthesize(text, speaker, speed):
    if speaker not in REF_INFO:
        raise ValueError(f"unknown speaker: {speaker!r} (available: {', '.join(sorted(REF_INFO))})")
    info = REF_INFO[speaker]
    ref_audio_path = os.path.join(_DATA_DIR, "ref_audio", os.path.basename(info["audio_path"]))

    model = get_model()
    t0 = time.perf_counter()
    wavs, sr = model.generate_voice_clone(
        text=text,
        ref_audio=ref_audio_path,
        ref_text=info["text"],
        **GENERATION_CONFIG,
    )
    print(f"tts-gpu: synth {(time.perf_counter() - t0) * 1000:.1f}ms, {len(text)} chars, speaker={speaker}", flush=True)
    audio = np.asarray(wavs[0], dtype=np.float32)

    if speed and speed != 1.0:
        # No native rate-control param (unlike Piper's length_scale or
        # Google TTS's speaking_rate) — resample instead, same tape-speed
        # trade-off as tts-generate's synthesize_vieneu.
        n_out = max(1, int(len(audio) / speed))
        audio = np.interp(
            np.linspace(0, len(audio) - 1, n_out),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)

    return audio, sr


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/tts/generate", methods=["POST"])
def generate():
    provided = request.headers.get("x-tts-secret", "")
    if not provided or not hmac.compare_digest(provided, TTS_API_SECRET):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    text = (body.get("text") or "").strip()
    if not text:
        return jsonify({"error": "No text"}), 400
    if len(text) > MAX_TEXT_CHARS:
        return jsonify({"error": f"text too long (max {MAX_TEXT_CHARS} chars)"}), 400

    try:
        speed = float(body.get("speed", 1.0))
    except (TypeError, ValueError):
        return jsonify({"error": "speed must be a number"}), 400

    speaker = (body.get("speaker") or DEFAULT_SPEAKER).strip()

    print(f"tts-gpu: speaker={speaker} speed={speed}: {text[:60]}", flush=True)

    try:
        audio, sr = synthesize(text, speaker, speed)
    except Exception as e:
        print(f"tts-gpu error: {e}", flush=True)
        return jsonify({"error": "Synthesis failed"}), 500

    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="wav", subtype="PCM_16")
    return buf.getvalue(), 200, {"Content-Type": "audio/wav"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
