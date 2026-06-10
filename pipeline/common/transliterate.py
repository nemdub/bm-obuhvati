"""Serbian Cyrillic <-> Latin transliteration.

The address register carries both scripts, but documents are Cyrillic-only and we
match in Cyrillic. The Latin<-Cyrillic direction is also used to fold house-number
suffixes (``190B`` -> ``190Б``) so register and document forms collapse together.
"""

from __future__ import annotations

import unicodedata

# Cyrillic -> Latin (Serbian, Gaj's Latin alphabet). Digraphs for љ/њ/џ.
_CYR_TO_LAT = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Ђ": "Đ", "Е": "E",
    "Ж": "Ž", "З": "Z", "И": "I", "Ј": "J", "К": "K", "Л": "L", "Љ": "Lj",
    "М": "M", "Н": "N", "Њ": "Nj", "О": "O", "П": "P", "Р": "R", "С": "S",
    "Т": "T", "Ћ": "Ć", "У": "U", "Ф": "F", "Х": "H", "Ц": "C", "Ч": "Č",
    "Џ": "Dž", "Ш": "Š",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "đ", "е": "e",
    "ж": "ž", "з": "z", "и": "i", "ј": "j", "к": "k", "л": "l", "љ": "lj",
    "м": "m", "н": "n", "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s",
    "т": "t", "ћ": "ć", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "č",
    "џ": "dž", "ш": "š",
}

# Latin -> Cyrillic. Order matters: match multi-char Latin digraphs before
# single letters. Keys are lowercase; casing is handled by the caller via upper().
_LAT_TO_CYR_PAIRS = [
    ("lj", "љ"), ("nj", "њ"), ("dž", "џ"), ("dž", "џ"),
    ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"), ("đ", "ђ"),
    ("e", "е"), ("ž", "ж"), ("z", "з"), ("i", "и"), ("j", "ј"), ("k", "к"),
    ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"), ("r", "р"),
    ("s", "с"), ("t", "т"), ("ć", "ћ"), ("u", "у"), ("f", "ф"), ("h", "х"),
    ("c", "ц"), ("č", "ч"), ("š", "ш"),
    # Common ASCII fallbacks (documents/exports sometimes drop diacritics).
    ("dj", "ђ"),
]


def nfc(s: str) -> str:
    """Normalize to NFC so precomposed/decomposed forms compare equal."""
    return unicodedata.normalize("NFC", s)


def cyr_to_lat(s: str) -> str:
    return "".join(_CYR_TO_LAT.get(ch, ch) for ch in nfc(s))


def lat_to_cyr(s: str) -> str:
    """Transliterate Latin to Cyrillic, honoring digraphs. Case-insensitive on
    input; returns lowercase Cyrillic. Intended for short tokens (suffixes)."""
    out: list[str] = []
    i = 0
    text = nfc(s).lower()
    while i < len(text):
        matched = False
        for lat, cyr in _LAT_TO_CYR_PAIRS:
            if text.startswith(lat, i):
                out.append(cyr)
                i += len(lat)
                matched = True
                break
        if not matched:
            out.append(text[i])
            i += 1
    return "".join(out)
