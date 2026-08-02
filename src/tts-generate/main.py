"""tts-generate — Piper TTS (Vietnamese), synchronous request/response.

Cloud Run version of the AWS Lambda tts-generate function (same
split_text/clean_text_for_tts/synthesize logic, same lazy-load-in-handler
pattern to keep cold start off any INIT-phase-style hard cap) — just wrapped
in Flask instead of a Lambda event, since Cloud Run expects a plain HTTP
server listening on $PORT.

Auth: header x-tts-secret must equal TTS_API_SECRET (shared secret, plain
env var — no Secret Manager, same choice made for the AWS deployment).
"""
import base64
import hmac
import io
import os
import re
import time

import numpy as np
import soundfile as sf
from flask import Flask, jsonify, request
from flask_cors import CORS
from piper.config import SynthesisConfig
from piper.voice import PiperVoice

TTS_API_SECRET = os.environ["TTS_API_SECRET"]

_MODEL_DIR = os.path.join(os.path.dirname(__file__), "piper_models")
_VOICE = "vi_VN-vais1000-medium"

MAX_TEXT_CHARS = 2000

app = Flask(__name__)
CORS(app, resources={r"/tts/*": {"origins": "*"}}, allow_headers=["Content-Type", "x-tts-secret"])

# Lazy-loaded, not module-scope — same reasoning as the Lambda version: keep
# heavy load (onnxruntime session + espeak-ng init) out of process startup
# so a slow environment degrades gracefully instead of risking a startup
# probe timeout, and so gunicorn workers only pay this cost on first request.
_voice = None


def _get_voice():
    global _voice
    if _voice is None:
        t0 = time.perf_counter()
        _voice = PiperVoice.load(
            os.path.join(_MODEL_DIR, f"{_VOICE}.onnx"),
            config_path=os.path.join(_MODEL_DIR, f"{_VOICE}.onnx.json"),
            use_cuda=False,
        )
        print(f"tts-generate: model load {(time.perf_counter() - t0) * 1000:.1f}ms", flush=True)
    return _voice


def clean_text_for_tts(text):
    text = re.sub(r"[·‧・•]", "", text)
    text = re.sub(r"[​-‍﻿]", "", text)
    text = text.replace("~", "")
    text = re.sub(r"\.{2,}", ".", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def split_text(text, max_chars=150):
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    chunks = []
    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= max_chars:
            chunks.append(sent)
        else:
            parts = re.split(r",\s*", sent)
            current = ""
            for part in parts:
                joined = (current + ", " + part) if current else part
                if len(joined) > max_chars and current:
                    chunks.append(current)
                    current = part
                else:
                    current = joined
            if current:
                chunks.append(current)
    return chunks or [text]


def synthesize(voice, text, speed=1.0):
    length_scale = 1.0 / speed if speed and speed != 1.0 else 1.0
    syn_config = SynthesisConfig(length_scale=length_scale)

    t0 = time.perf_counter()
    sentence_phonemes = voice.phonemize(text)
    t_phon = (time.perf_counter() - t0) * 1000

    t_ids = 0.0
    t_infer = 0.0
    parts = []
    for phonemes in sentence_phonemes:
        if not phonemes:
            continue

        t0 = time.perf_counter()
        phoneme_ids = voice.phonemes_to_ids(phonemes)
        t_ids += (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        audio = voice.phoneme_ids_to_audio(phoneme_ids, syn_config)
        t_infer += (time.perf_counter() - t0) * 1000

        # Mirrors PiperVoice.synthesize()'s default normalize_audio=True / volume=1.0.
        max_val = np.max(np.abs(audio))
        audio = np.zeros_like(audio) if max_val < 1e-8 else audio / max_val
        parts.append(np.clip(audio, -1.0, 1.0).astype(np.float32))

    print(f"tts-generate: synth breakdown phonemize={t_phon:.1f}ms ids={t_ids:.1f}ms infer={t_infer:.1f}ms", flush=True)

    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts)


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

    voice = _get_voice()
    sample_rate = voice.config.sample_rate

    chunks = split_text(clean_text_for_tts(text))
    print(f"tts-generate: {len(chunks)} chunk(s), speed={speed}: {text[:60]}", flush=True)

    silence = np.zeros(int(sample_rate * 0.25), dtype=np.float32)
    parts = []
    for chunk in chunks:
        try:
            parts.append(synthesize(voice, chunk, speed=speed))
            parts.append(silence)
        except Exception as e:
            print(f"Chunk error: {e}", flush=True)

    if not parts:
        return jsonify({"error": "Synthesis failed"}), 500

    wav = np.concatenate(parts)
    buf = io.BytesIO()
    sf.write(buf, wav, sample_rate, format="wav", subtype="PCM_16")

    return buf.getvalue(), 200, {"Content-Type": "audio/wav"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
