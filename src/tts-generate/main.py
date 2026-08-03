"""tts-generate — Piper TTS (Vietnamese + English) + Google Cloud TTS,
synchronous request/response.

Cloud Run version of the AWS Lambda tts-generate function (same
split_text/clean_text_for_tts/synthesize logic, same lazy-load-in-handler
pattern to keep cold start off any INIT-phase-style hard cap) — just wrapped
in Flask instead of a Lambda event, since Cloud Run expects a plain HTTP
server listening on $PORT.

Piper's `vi` phonemizer garbles inline English (character names,
brand/platform names) that's constant in translated web novels. There's no
single fix that's clearly best — routing English to a second Piper voice
reads it correctly but sounds like a different speaker cutting in
mid-sentence (jarring); staying on one voice avoids that but mispronounces
foreign names. So the request picks a MODEL (see MODELS below) instead of
the server deciding unilaterally:

  piper_vi           single Piper vi voice, no segmentation (default,
                      original pre-fix behavior — safest, $0)
  piper_vi_translit   single Piper vi voice, English runs phonetically
                      respelled into Vietnamese orthography first
                      (transliterate.py) — no voice switch, imperfect
                      pronunciation instead
  piper_vi_en         Piper vi + en voices, routed per-run
                      (lang_segment.segment_by_language) — correct English
                      pronunciation, jarring voice switch
  google_tts          Google Cloud TTS, single Vietnamese voice, whole
                      text in one call, no segmentation — tests whether a
                      much larger/more diverse commercial voice already
                      handles inline English acceptably on its own
  vieneu              VieNeu-TTS (open-source, Apache-2.0), single
                      multilingual model natively handles VI/EN
                      code-switching — no segmentation/transliteration
                      needed, whole text in one call like google_tts

Auth: header x-tts-secret must equal TTS_API_SECRET (shared secret, plain
env var — no Secret Manager, same choice made for the AWS deployment).
"""
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

from lang_segment import segment_by_language
from transliterate import transliterate_text

TTS_API_SECRET = os.environ["TTS_API_SECRET"]

MODELS = {"piper_vi", "piper_vi_translit", "piper_vi_en", "google_tts", "vieneu"}
DEFAULT_MODEL = "piper_vi"

_MODEL_DIR = os.path.join(os.path.dirname(__file__), "piper_models")

# Per-language voice registry — deliberately a dict, not a single hardcoded
# voice, so adding a third language later (e.g. Chinese names common in
# wuxia-genre sources) is a one-line addition, not a rewrite. All voices
# here must share PRIMARY_LANG's sample rate (checked in get_voice below)
# since runs get np.concatenate'd directly with no resampling step.
VOICES = {
    "vi": "vi_VN-vais1000-medium",
    "en": "en_US-lessac-medium",
}
PRIMARY_LANG = "vi"  # unresolvable/no-letter text segments default here

MAX_TEXT_CHARS = 2000

# google_tts mode config — Standard tier (cheapest, $4/1M chars, 4M
# free/month) to start; swap to a Neural2/Chirp3-HD voice name later if
# quality demands it and the cost is acceptable. Female voice to match
# the Piper voices' character (vais1000 is also female).
GOOGLE_TTS_VOICE = os.environ.get("GOOGLE_TTS_VOICE", "vi-VN-Standard-A")

# vieneu mode config — "Đoan Trang" (Nữ · Bắc · tự nhiên) picked as a
# reasonable female/natural-style default among the 14 built-in presets,
# matching the other modes' voice character; override via env if a
# different preset reads better.
VIENEU_VOICE = os.environ.get("VIENEU_VOICE", "Đoan Trang")
VIENEU_STYLE = os.environ.get("VIENEU_STYLE", "tu_nhien")

app = Flask(__name__)
CORS(app, resources={r"/tts/*": {"origins": "*"}}, allow_headers=["Content-Type", "x-tts-secret"])

# Lazy-loaded PER LANGUAGE, not eagerly for the whole registry — same
# cold-start reasoning as the single-voice version, extended: a pure-
# Vietnamese chapter (the common case) never pays to load the English
# voice at all, since get_voice() is only called for languages that
# actually show up in a given request's segmented runs.
_loaded_voices = {}


def get_voice(lang):
    voice_name = VOICES.get(lang, VOICES[PRIMARY_LANG])
    if voice_name not in _loaded_voices:
        t0 = time.perf_counter()
        voice = PiperVoice.load(
            os.path.join(_MODEL_DIR, f"{voice_name}.onnx"),
            config_path=os.path.join(_MODEL_DIR, f"{voice_name}.onnx.json"),
            use_cuda=False,
        )
        print(f"tts-generate: model load lang={lang} ({voice_name}) {(time.perf_counter() - t0) * 1000:.1f}ms", flush=True)
        if _loaded_voices and voice.config.sample_rate != next(iter(_loaded_voices.values())).config.sample_rate:
            # Would corrupt audio (pitch-shifted) if concatenated as-is —
            # fail loud rather than silently produce broken output. Add
            # resampling (scipy.signal.resample, precedented in
            # kokoro-tts-addon/server.py's ViXTTSBackend) if this ever fires.
            raise RuntimeError(f"voice {voice_name!r} sample rate mismatch — resampling not implemented")
        _loaded_voices[voice_name] = voice
    return _loaded_voices[voice_name]


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


def synthesize_chunk(text, speed, sample_rate):
    """Segments `text` by language and synthesizes each run with the
    matching voice, stitching them back into one chunk's audio. A short
    pause (not the full inter-*chunk* silence used between sentences —
    that'd sound choppy between sub-sentence runs) marks each language
    switch so the cadence stays natural."""
    runs = segment_by_language(text)
    switch_pause = np.zeros(int(sample_rate * 0.07), dtype=np.float32)

    parts = []
    for i, (lang, run_text) in enumerate(runs):
        if i > 0:
            parts.append(switch_pause)
        voice = get_voice(lang)
        parts.append(synthesize(voice, run_text, speed=speed))

    if not parts:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(parts)


def synthesize_translit_chunk(text, speed, sample_rate):
    """Segments `text` by language, phonetically respells English runs
    into Vietnamese orthography (transliterate.transliterate_text), then
    synthesizes the WHOLE result as one string with the single vi voice —
    no run-stitching/pauses needed since it's genuinely one continuous
    string for one voice by this point."""
    runs = segment_by_language(text)
    respelled = " ".join(
        transliterate_text(run_text) if lang == "en" else run_text
        for lang, run_text in runs
    )
    if respelled != text:
        print(f"tts-generate: translit {text!r} -> {respelled!r}", flush=True)
    return synthesize(get_voice(PRIMARY_LANG), respelled, speed=speed)


_google_tts_client = None


def _get_google_tts_client():
    global _google_tts_client
    if _google_tts_client is None:
        from google.cloud import texttospeech
        # REST, not the default gRPC transport — gRPC's connection pattern
        # gets blocked in some sandboxed network environments (confirmed:
        # DNS resolution for the gRPC channel failed locally while a plain
        # REST call to the same API succeeded); REST works everywhere gRPC
        # does for this API, just marginally less efficient, so hardcoding
        # it is a safe default rather than something to special-case.
        _google_tts_client = texttospeech.TextToSpeechClient(transport="rest")
    return _google_tts_client


def synthesize_google(text, speed):
    """Single call, whole text, no segmentation — returns (wav_bytes,
    mime_type). LINEAR16 encoding from this API is WAV-container PCM, not
    headerless, so the response can be returned to the client as-is."""
    from google.cloud import texttospeech

    client = _get_google_tts_client()
    t0 = time.perf_counter()
    response = client.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(language_code="vi-VN", name=GOOGLE_TTS_VOICE),
        audio_config=texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.LINEAR16,
            speaking_rate=max(0.25, min(4.0, speed)),  # API's documented valid range
        ),
    )
    print(f"tts-generate: google_tts synth {(time.perf_counter() - t0) * 1000:.1f}ms, {len(text)} chars", flush=True)
    return response.audio_content, "audio/wav"


_vieneu_tts = None


def _get_vieneu():
    global _vieneu_tts
    if _vieneu_tts is None:
        import vieneu
        t0 = time.perf_counter()
        _vieneu_tts = vieneu.Vieneu(mode="v3turbo")
        print(f"tts-generate: vieneu model load {(time.perf_counter() - t0) * 1000:.1f}ms", flush=True)
    return _vieneu_tts


def synthesize_vieneu(text, speed):
    """Single call, whole text — vieneu's infer() already splits long text
    into chunks internally (max_chars=256) and stitches them with
    silence/crossfade, so no split_text/silence-join loop is needed here,
    same reasoning as synthesize_google. Handles VI/EN code-switching
    natively (no lang_segment/transliterate step) since it's one
    multilingual model, not a per-language Piper voice pair."""
    tts = _get_vieneu()
    t0 = time.perf_counter()
    audio = tts.infer(text, voice=VIENEU_VOICE, style=VIENEU_STYLE)
    print(f"tts-generate: vieneu synth {(time.perf_counter() - t0) * 1000:.1f}ms, {len(text)} chars", flush=True)

    if speed and speed != 1.0:
        # infer() has no native rate-control param (unlike Piper's
        # length_scale or Google TTS's speaking_rate) — resample instead,
        # which shifts pitch along with duration (tape-speed style). A
        # known trade-off, not a bug: true time-stretch would need a phase
        # vocoder, not worth the extra dependency for a mode still being
        # evaluated.
        n_out = max(1, int(len(audio) / speed))
        audio = np.interp(
            np.linspace(0, len(audio) - 1, n_out),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)

    buf = io.BytesIO()
    sf.write(buf, audio, tts.sample_rate, format="wav", subtype="PCM_16")
    return buf.getvalue(), "audio/wav"


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

    model = (body.get("model") or DEFAULT_MODEL).strip()
    if model not in MODELS:
        return jsonify({"error": f"invalid model: {model!r} (allowed: {', '.join(sorted(MODELS))})"}), 400

    print(f"tts-generate: model={model} speed={speed}: {text[:60]}", flush=True)

    if model in ("google_tts", "vieneu"):
        try:
            if model == "google_tts":
                audio_bytes, mime = synthesize_google(text, speed)
            else:
                audio_bytes, mime = synthesize_vieneu(text, speed)
        except Exception as e:
            print(f"{model} error: {e}", flush=True)
            return jsonify({"error": "Synthesis failed"}), 500
        return audio_bytes, 200, {"Content-Type": mime}

    # Piper-based modes (piper_vi / piper_vi_translit / piper_vi_en) share
    # the same chunk/silence-stitch skeleton, differing only in how each
    # chunk gets synthesized. Only the primary (Vietnamese) voice loads
    # here — get_voice(lang) for any other language is deferred until a
    # run actually needs it, so a pure-Vietnamese chapter (the common
    # case) never pays to load the English voice at all, in every mode.
    sample_rate = get_voice(PRIMARY_LANG).config.sample_rate

    chunks = split_text(clean_text_for_tts(text))
    print(f"tts-generate: {len(chunks)} chunk(s)", flush=True)

    silence = np.zeros(int(sample_rate * 0.25), dtype=np.float32)
    parts = []
    for chunk in chunks:
        try:
            if model == "piper_vi":
                audio = synthesize(get_voice(PRIMARY_LANG), chunk, speed=speed)
            elif model == "piper_vi_translit":
                audio = synthesize_translit_chunk(chunk, speed, sample_rate)
            else:  # piper_vi_en
                audio = synthesize_chunk(chunk, speed, sample_rate)
            parts.append(audio)
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
