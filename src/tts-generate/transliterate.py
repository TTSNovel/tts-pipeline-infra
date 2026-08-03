"""English -> Vietnamese-orthography phonetic respelling (mode: piper_vi_translit).

Uses Piper's already-bundled espeak-ng English (en-us) phonemizer to get
real IPA phonemes for an English word, then maps IPA -> a rough
Vietnamese-spelling approximation via a static table, so the word can be
read by the SAME Vietnamese Piper voice instead of switching to a
different voice — fixes the jarring voice-swap in piper_vi_en mode, at the
cost of pronunciation fidelity. This is a best-effort v1: Vietnamese
doesn't have exact equivalents for several English phonemes (æ, ʌ, ə, θ,
ð, ʃ, ʒ...), so the mapping is nearest-sound, not phonetically precise.

Auto-caches word -> respelling in GCS (NOT a hand-curated dictionary) —
new words are transliterated on first encounter and written back, giving
visibility into what's been auto-generated without anyone maintaining a
dictionary by hand.
"""
import json
import logging
import os
import re

from piper.phonemize_espeak import ESPEAK_DATA_DIR, EspeakPhonemizer

try:
    from google.cloud import storage
except ImportError:
    storage = None

logger = logging.getLogger(__name__)

_CACHE_BUCKET = os.environ.get("TTS_ASSETS_BUCKET", "tts-pipeline-yl-tts-assets")
_CACHE_BLOB = "transliteration_cache.json"

# IPA (espeak-ng en-us output) -> Vietnamese-orthography approximation.
# Longer symbols (diphthongs, affricates) must be checked before their
# component single symbols — see _phonemes_to_vi_spelling.
_IPA_TO_VI = {
    # affricates / diphthongs (multi-symbol — matched greedily first)
    "tʃ": "ch", "dʒ": "gi",
    "eɪ": "ây", "aɪ": "ai", "aʊ": "ao", "ɔɪ": "oi", "oʊ": "ô",
    # consonants
    "p": "p", "b": "b", "t": "t", "d": "đ", "k": "c", "ɡ": "g", "g": "g",
    "f": "ph", "v": "v", "s": "s", "z": "d", "h": "h",
    "m": "m", "n": "n", "ŋ": "ng", "l": "l", "ɹ": "r", "r": "r", "ɾ": "r",
    "θ": "th", "ð": "đ", "ʃ": "s", "ʒ": "d",
    "j": "i", "w": "u",
    # vowels
    "i": "i", "ɪ": "i",
    "e": "ê", "ɛ": "e",
    "æ": "e", "a": "a", "ɑ": "a",
    "ʌ": "ă", "ə": "ơ", "ɜ": "ơ", "ᵻ": "ơ", "ɐ": "ơ",
    "ɔ": "o",
    "ʊ": "u", "u": "u",
    # stress/length markers — dropped (Vietnamese tone marks are a
    # different dimension, not applied here in this v1)
    "ˈ": "", "ˌ": "", "ː": "",
}
_IPA_KEYS_BY_LEN = sorted(_IPA_TO_VI, key=len, reverse=True)

# Which mapped pieces count as a syllable nucleus (vowel) — used to insert
# syllable-boundary spaces below. Vietnamese orthography is written ONE
# SPACE-SEPARATED TOKEN PER SYLLABLE (e.g. "Việt Nam" = 2 tokens) — Piper's
# vi phonemizer expects that shape. Concatenating a whole English word's
# mapped phonemes into a single unbroken token (e.g. "phulmerơl" for
# "Fullmetal") produces a "word" that doesn't match any real Vietnamese
# syllable, so the phonemizer has to fall back to some generic guess for
# it — which can end up sounding barely different from just feeding it the
# raw untransliterated English word. Splitting into syllable-shaped,
# space-separated chunks is what actually lets the vi voice read it as
# Vietnamese-style speech instead of another unrecognized blob.
_VOWEL_PIECES = {"i", "ê", "e", "a", "ă", "ơ", "o", "ô", "u", "ai", "ao", "oi", "ây"}


def _phonemes_to_vi_spelling(phoneme_str):
    """Greedy longest-match over the concatenated phoneme string (espeak
    emits one symbol per list element, but multi-char IPA symbols like
    affricates/diphthongs may span what we treat as separate elements —
    matching against the joined string with longest-symbol-first handles
    both cases uniformly), then grouped into syllable-shaped, space-
    separated chunks: a syllable closes right after its vowel nucleus, so
    a run of onset consonants attaches to the vowel that follows them, and
    any consonant(s) left after the last vowel attach to the final
    syllable as a coda — a rough approximation of Vietnamese syllable
    shape, not a precise English-syllabification algorithm."""
    pieces = []
    i = 0
    while i < len(phoneme_str):
        for key in _IPA_KEYS_BY_LEN:
            if phoneme_str.startswith(key, i):
                mapped = _IPA_TO_VI[key]
                if mapped:
                    pieces.append(mapped)
                i += len(key)
                break
        else:
            i += 1  # unmapped symbol — drop it rather than crash

    syllables = []
    current = ""
    for piece in pieces:
        current += piece
        if piece in _VOWEL_PIECES:
            syllables.append(current)
            current = ""
    if current:
        if syllables:
            syllables[-1] += current  # trailing consonant(s) -> coda of last syllable
        else:
            syllables.append(current)  # no vowel found at all — keep as-is

    return " ".join(syllables)


_phonemizer = None


def _get_phonemizer():
    global _phonemizer
    if _phonemizer is None:
        _phonemizer = EspeakPhonemizer(ESPEAK_DATA_DIR)
    return _phonemizer


_cache = None
_cache_dirty = False


def _load_cache():
    if storage is None:
        return {}
    try:
        client = storage.Client()
        blob = client.bucket(_CACHE_BUCKET).blob(_CACHE_BLOB)
        if blob.exists():
            return json.loads(blob.download_as_text())
    except Exception as e:
        logger.warning(f"transliterate: cache load failed: {e}")
    return {}


def _flush_cache():
    """Best-effort write-back — called once per request (not per word) by
    transliterate_text(), not on every transliterate_word() call, so a
    sentence with several new words costs one GCS write, not several."""
    global _cache_dirty
    if not _cache_dirty or storage is None:
        return
    try:
        client = storage.Client()
        blob = client.bucket(_CACHE_BUCKET).blob(_CACHE_BLOB)
        blob.upload_from_string(
            json.dumps(_cache, ensure_ascii=False, indent=2, sort_keys=True),
            content_type="application/json",
        )
        _cache_dirty = False
    except Exception as e:
        logger.warning(f"transliterate: cache save failed: {e}")


def transliterate_word(word):
    """Vietnamese-orthography approximation of one English word's
    pronunciation, via espeak-ng phonemes -> IPA->VI table. Leading/
    trailing punctuation is preserved around the respelled core so
    Piper's vi voice still gets sentence-prosody cues (pauses on commas
    etc.) instead of losing them."""
    global _cache, _cache_dirty
    if _cache is None:
        _cache = _load_cache()

    m = re.match(r"^(\W*)(\w+)(\W*)$", word, re.UNICODE)
    if not m:
        return word  # pure punctuation/whitespace token — nothing to respell
    prefix, core, suffix = m.groups()

    key = core.lower()
    if key in _cache:
        spelling = _cache[key]
    else:
        phoneme_groups = _get_phonemizer().phonemize("en-us", core)
        phoneme_str = "".join(p for group in phoneme_groups for p in group)
        spelling = _phonemes_to_vi_spelling(phoneme_str) or core
        _cache[key] = spelling
        _cache_dirty = True

    return prefix + spelling + suffix


def transliterate_text(text):
    """Respell every word in `text` (an isolated English run, per
    lang_segment.segment_by_language) and flush any newly-generated cache
    entries once for the whole call."""
    result = " ".join(transliterate_word(w) for w in text.split())
    _flush_cache()
    return result
