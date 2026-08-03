"""Per-token Vietnamese/English language segmentation.

Fixes Piper's `vi` phonemizer garbling inline English (character names,
brand/platform names, tech terms) that shows up constantly in translated
web novels — by splitting text into consecutive same-language runs so each
run can be routed to the matching Piper voice (see main.py's VOICES
registry) instead of forcing everything through the Vietnamese phonemizer.

Vietnamese is a closed-syllable-inventory language, which makes a cheap,
accurate-enough heuristic possible without per-token ML language ID (too
slow for this latency budget):

  1. A token containing a Vietnamese diacritic/tone mark is unambiguously
     Vietnamese — no lookup needed.
  2. A token with no diacritics is AMBIGUOUS, not automatically Vietnamese
     — e.g. "on"/"in"/"at"/"man"/"Titan" all happen to collide with real
     diacritic-stripped Vietnamese syllables ("on" == "ơn" stripped, as in
     "cảm ơn"), so membership in vi_syllables.txt alone isn't reliable
     evidence either way (measured: 54% of common English function
     words/short names collide). Ambiguous tokens instead inherit
     whichever language their nearest already-resolved neighbor has — see
     segment_by_language()'s forward-fill below — which correctly keeps
     "Attack on Titan!" together as English instead of "on"/"Titan"
     flipping back to Vietnamese mid-phrase, while still resolving bare
     Vietnamese words correctly in real prose (which almost always has a
     diacritic-bearing anchor nearby).
  3. Tokens with no letters at all (numbers, punctuation) are likewise
     language-less — same neighbor-inheritance handles them.

vi_syllables.txt: ~3000 unique bare syllables extracted from
underthesea's Viet74K.txt wordlist (Apache-2.0) — see that file's header
for exactly how it was derived.
"""
import os
import re
import unicodedata

_VI_DIACRITIC_RE = re.compile(
    "[ăâđêôơưƯ"
    "àằầèềìòồờùừỳ"
    "áắấéếíóốớúứý"
    "ảẳẩẻểỉỏổởủửỷ"
    "ãẵẫẽễĩõỗỡũữỹ"
    "ạặậẹệịọộợụựỵ]",
    re.IGNORECASE,
)

_SYLLABLES_PATH = os.path.join(os.path.dirname(__file__), "vi_syllables.txt")


def _load_vi_syllables(path=_SYLLABLES_PATH):
    syllables = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                syllables.add(line)
    return syllables


VI_SYLLABLES = _load_vi_syllables()


def _strip_diacritics(s):
    s = s.replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", s)


def _token_lang(token):
    """Classify one whitespace-delimited token as:
      'vi'  — unambiguous (has a Vietnamese diacritic/tone mark)
      'en'  — unambiguous (bare letters, NOT a known Vietnamese syllable)
      None  — ambiguous or language-less: no letters at all, OR bare
              letters that happen to match a real Vietnamese syllable
              (could be that word, or a coincidentally-matching English
              word — see module docstring). Resolved by neighbor
              inheritance in segment_by_language(), not guessed here.
    """
    letters = "".join(c for c in token if c.isalpha())
    if not letters:
        return None
    if _VI_DIACRITIC_RE.search(letters):
        return "vi"
    bare = _strip_diacritics(letters).lower()
    if bare in VI_SYLLABLES:
        return None
    return "en"


def segment_by_language(text):
    """Split text into consecutive (lang, run_text) tuples, lang in
    {'vi', 'en'}. Runs are whitespace-joined (minor whitespace
    normalization, not byte-exact reconstruction — fine for TTS input)."""
    words = text.split()
    if not words:
        return []

    langs = [_token_lang(w) for w in words]

    # None tokens (language-less: numbers/punctuation — or ambiguous: bare
    # letters matching a real VI syllable) inherit the previous resolved
    # run's language; any leading run of them (before the first unambiguous
    # token) inherits the first language that does appear, defaulting to
    # 'vi' if the whole chunk never resolves (e.g. it's all digits, or all
    # ambiguous with no unambiguous anchor anywhere).
    first_known = next((l for l in langs if l is not None), "vi")
    resolved = []
    last = first_known
    for lang in langs:
        if lang is not None:
            last = lang
        resolved.append(last)

    runs = []
    cur_lang, cur_words = resolved[0], [words[0]]
    for lang, word in zip(resolved[1:], words[1:]):
        if lang == cur_lang:
            cur_words.append(word)
        else:
            runs.append((cur_lang, " ".join(cur_words)))
            cur_lang, cur_words = lang, [word]
    runs.append((cur_lang, " ".join(cur_words)))
    return runs
