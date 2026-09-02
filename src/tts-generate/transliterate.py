"""English -> Vietnamese-orthography phonetic respelling (mode: piper_vi_translit).

Uses vietnormalizer's curated English/loanword dictionary (17.7K entries:
brand names, tech terms, common vocabulary) for known words, falling back to
its rule-based english_to_vietnamese() — pure orthographic regex rules, no
phonemizer involved — for anything not in the dictionary. Replaces the
previous espeak-IPA-based approach (same GCS auto-cache as before, now keyed
on vietnormalizer's output instead of the homegrown IPA table's); dropped
the piper.phonemize_espeak dependency this mode no longer needs.

Deliberately calls vietnormalizer.transliterator.english_to_vietnamese()
directly, NOT vietnormalizer.transliterate_word() / VietnameseNormalizer —
both of those gate on vietnormalizer's own is_vietnamese_word(), a pure
phonotactic-shape heuristic with no sentence context (e.g. "long" is both a
legal Vietnamese syllable and an English word; that gate always resolves it
to Vietnamese regardless of which one it actually is in context — confirmed
by vietnormalizer's own dictionary separately mapping "long" -> "lon", which
its gate then never lets fire). lang_segment.segment_by_language already
resolved this run as English using neighbor-inherited sentence context,
which is strictly more accurate for exactly these collision cases (measured
54% collision rate for short bare-Latin tokens, see lang_segment.py) —
re-running vietnormalizer's own gate here would silently override that
better decision with a worse one.
"""
import csv
import json
import logging
import os
import re

from vietnormalizer.transliterator import english_to_vietnamese

try:
    from google.cloud import storage
except ImportError:
    storage = None

logger = logging.getLogger(__name__)

# No hardcoded fallback — an unset TTS_ASSETS_BUCKET just disables the GCS
# cache below (best-effort already, see _load_cache/_flush_cache) rather than
# silently pointing at a specific personal bucket.
_CACHE_BUCKET = os.environ.get("TTS_ASSETS_BUCKET")
_CACHE_BLOB = "transliteration_cache.json"

_dictionary = None


def _get_dictionary():
    """vietnormalizer's curated English/loanword -> Vietnamese-spelling
    dictionary, read straight from its bundled CSV rather than via
    VietnameseNormalizer/VietnameseTextProcessor — skips compiling that
    class's number/date/currency/acronym regexes, which this mode never
    uses and which cold-start latency here can't afford for free."""
    global _dictionary
    if _dictionary is None:
        import vietnormalizer

        data_dir = os.path.join(os.path.dirname(vietnormalizer.__file__), "data")
        _dictionary = {}
        with open(os.path.join(data_dir, "non-vietnamese-words.csv"), encoding="utf-8") as f:
            for row in csv.DictReader(f):
                _dictionary[row["original"].strip().lower()] = row["transliteration"].strip()
    return _dictionary


_cache = None
_cache_dirty = False


def _load_cache():
    if storage is None or _CACHE_BUCKET is None:
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
    if not _cache_dirty or storage is None or _CACHE_BUCKET is None:
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
    pronunciation: a vietnormalizer dictionary hit if it has one curated,
    else its rule-based english_to_vietnamese() fallback. Leading/
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
        spelling = _get_dictionary().get(key) or english_to_vietnamese(core) or core
        # vietnormalizer joins syllables with hyphens ("mê-lít-xa"), but
        # Piper's vi phonemizer expects one SPACE-separated token per
        # syllable to recognize each as a real Vietnamese syllable — a
        # hyphen-glued blob gets treated as one unrecognized "word" and
        # falls back to a generic, IPA-ish phonemization instead of clean
        # per-syllable Vietnamese pronunciation (confirmed by ear: "mê-lít-xa"
        # vs "mê lít xa" through the same vi voice sound audibly different).
        spelling = spelling.replace("-", " ")
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
