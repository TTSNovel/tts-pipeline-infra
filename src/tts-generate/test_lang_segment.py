"""Standalone sanity check for lang_segment.segment_by_language() — no
pytest dependency, no Piper/onnxruntime import, run directly:

    python3 test_lang_segment.py

Covers: pure Vietnamese, pure English, mixed runs, ambiguous bare-syllable
Vietnamese words that also look like English ("la", "co", "an" — must NOT
be misclassified as English), numbers attaching to the adjacent run, and
realistic mixed sentences from the actual translated-web-novel domain this
fixes (character/platform names inline in Vietnamese prose).
"""
from lang_segment import segment_by_language

CASES = [
    (
        "Cố Tức mở Discord lên xem tin nhắn từ John.",
        [("vi", "Cố Tức mở"), ("en", "Discord"), ("vi", "lên xem tin nhắn từ"), ("en", "John.")],
    ),
    (
        "Anh ta đăng nhập Facebook rồi nhắn tin cho Sarah về việc gặp mặt.",
        [
            ("vi", "Anh ta đăng nhập"),
            ("en", "Facebook"),
            ("vi", "rồi nhắn tin cho"),
            ("en", "Sarah"),
            ("vi", "về việc gặp mặt."),
        ],
    ),
    (
        "Chương 61: Cố Tức bước vào quán rượu.",
        [("vi", "Chương 61: Cố Tức bước vào quán rượu.")],
    ),
    (
        "Hôm nay trời đẹp, tôi đi dạo công viên.",
        [("vi", "Hôm nay trời đẹp, tôi đi dạo công viên.")],
    ),
    (
        "Hello world, this is English only.",
        [("en", "Hello world, this is English only.")],
    ),
    # "la", "co", "an" are valid bare Vietnamese syllables (no diacritics)
    # that ALSO look like English — must resolve as Vietnamese via the
    # syllable-list lookup, not misfire as English.
    (
        "Anh la lên gọi co bạn đến an toàn.",
        [("vi", "Anh la lên gọi co bạn đến an toàn.")],
    ),
    (
        # "laptop" isn't in the underlying dictionary (a formal Vietnamese
        # lexicon, not a colloquial tech-loanword glossary) — known,
        # documented limitation: uncommon/informal loanwords not covered by
        # the source wordlist route to the English voice rather than vi.
        "Cô ấy mở laptop, vào Google tìm kiếm thông tin về Elon Musk.",
        [
            ("vi", "Cô ấy mở"),
            ("en", "laptop,"),
            ("vi", "vào"),
            ("en", "Google"),
            ("vi", "tìm kiếm thông tin về"),
            ("en", "Elon Musk."),
        ],
    ),
    # Regression: "on"/"Titan" are bare and both collide with real
    # diacritic-stripped Vietnamese syllables ("on" == "ơn" as in "cảm
    # ơn"), so they used to independently misclassify as Vietnamese mid-
    # phrase. Now ambiguous tokens inherit the nearest resolved neighbor
    # ("Attack" is unambiguously English), keeping "Attack on Titan!"
    # together as one English run.
    (
        "liệp sát giả sẽ tại hai mười lăm điểm sau tiến nhập diễn sinh thế giới: Attack on Titan!",
        [
            ("vi", "liệp sát giả sẽ tại hai mười lăm điểm sau tiến nhập diễn sinh thế giới:"),
            ("en", "Attack on Titan!"),
        ],
    ),
    # Documented residual limitation, not a regression: a collision-word
    # name ("Tom"/"Mark" — both bare, both happen to match real VI
    # syllables) with NO adjacent unambiguous-English token anywhere in
    # the chunk can't be resolved as English — it falls back to the
    # Vietnamese default, same as any other ambiguous token with no
    # anchor. Fixing this would need real named-entity recognition, out
    # of scope for a per-token heuristic.
    (
        "Tom đi học.",
        [("vi", "Tom đi học.")],
    ),
    (
        "Anh ta gặp Mark ở sân bay.",
        [("vi", "Anh ta gặp Mark ở sân bay.")],
    ),
]


def run():
    failures = 0
    for text, expected in CASES:
        actual = segment_by_language(text)
        status = "OK" if actual == expected else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"[{status}] {text!r}")
        if status == "FAIL":
            print(f"    expected: {expected}")
            print(f"    actual:   {actual}")

    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    run()
