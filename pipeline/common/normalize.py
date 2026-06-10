"""Normalization helpers shared by parsing (stage03/03b) and matching (stage04).

All matching happens in Cyrillic. House numbers are split into a numeric part and
a normalized Cyrillic suffix so that ``190``, ``190Б``, ``190-Б`` and the register's
Latin ``190B`` all line up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .transliterate import lat_to_cyr, nfc

# Street-name abbreviation expansions (normalized, uppercase Cyrillic forms).
_STREET_ABBREV = {
    "Ј.Н.А.": "ЈНА",
    "ЈНА.": "ЈНА",
    "БР.": "",
    "УЛ.": "",
    "ДР.": "ДР",
}

_WS_RE = re.compile(r"\s+")
_STREET_LABEL_RE = re.compile(r"^\s*УЛИЦА\s*:?\s*", re.IGNORECASE)
# "Др" (doctor) abbreviation: docs write "Др Ђорђа Лазића", the register spells out
# "ДОКТОРА ЂОРЂА ЛАЗИЋА". Expanded on both sides so they converge.
_DR_RE = re.compile(r"\bДР\b")
# Roman numerals (Latin letters, values 1-39 — street ordinals like VII, XII): docs and the
# register disagree on Roman vs Arabic ("XII војвођанске..." vs "12.ВОЈВОЂАНСКЕ...",
# and the inverse "8. војвођанске" vs "VIII ВОЈВОЂАНСКА"). Normalized to Arabic on both
# sides. Strict 1-39 pattern so Latin-lettered tokens that aren't numerals never convert.
_ROMAN_RE = re.compile(r"\b(X{0,3})(IX|IV|V?I{0,3})\b")
_ROMAN_VAL = {"I": 1, "V": 5, "X": 10}


def _roman_to_arabic(m: re.Match) -> str:
    s = m.group(0)
    if not s:
        return s
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN_VAL[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return str(total)
# Latin letters that may appear in suffixes coming from the register's Latin column.
_HAS_LATIN_RE = re.compile(r"[A-Za-zĐŽĆČŠđžćčš]")


def normalize_street(name: str) -> str:
    """Build the Cyrillic matching key for a street name.

    NFC -> strip 'Улица:' label -> uppercase -> expand abbreviations ->
    drop punctuation -> collapse whitespace.
    """
    s = nfc(name)
    s = _STREET_LABEL_RE.sub("", s)
    s = s.upper()
    for abbr, full in _STREET_ABBREV.items():
        s = s.replace(abbr, full)
    # Keep Cyrillic/Latin letters, digits and spaces; drop other punctuation.
    s = re.sub(r"[^0-9A-Za-zА-Яа-яЂЃЄЅІЇЈЉЊЋЌЎЏђѓєѕіїјљњћќўџ\s]", " ", s)
    s = _DR_RE.sub("ДОКТОРА", s)
    s = _ROMAN_RE.sub(_roman_to_arabic, s)
    s = _WS_RE.sub(" ", s).strip()
    return s


_CYR_VOWELS = set("АЕИОУ")
_MAX_GEN_VARIANTS = 16


def _word_case_options(w: str) -> list[str]:
    """Declension options for one word of a street name (the word itself always first).

    Serbian street names mix nominative and genitive across sources, and Hungarian
    names decline with the stem kept: А-stems НИКОЛА<->НИКОЛЕ; consonant ВУК->ВУКА;
    О-final ДАНКО->ДАНКОА (Hungarian style) or БРАНКО->БРАНКА (Serbian style);
    Е-final ЂОРЂЕ->ЂОРЂА."""
    if len(w) < 4 or any(ch.isdigit() for ch in w):
        return [w]
    last = w[-1]
    if last == "А":
        return [w, w[:-1] + "Е"]
    if last == "О":
        return [w, w + "А", w[:-1] + "А"]
    if last == "Е":
        return [w, w[:-1] + "А"]
    if last not in _CYR_VOWELS and last.isalpha():
        return [w, w + "А"]
    return [w]


def genitive_variants(norm: str) -> list[str]:
    """All declension variants of a normalized street name (excluding the name itself),
    capped to keep combinatorics bounded. Used only as ALTERNATE settlement-scoped keys
    (they never replace literal names, never municipality-wide)."""
    from itertools import product

    options = [_word_case_options(w) for w in norm.split()]
    out: list[str] = []
    for combo in product(*options):
        v = " ".join(combo)
        if v != norm:
            out.append(v)
            if len(out) >= _MAX_GEN_VARIANTS:
                break
    return out


def genitive_variant(norm: str) -> str | None:
    """First (most common) declension variant, or None. Kept for callers that only need
    the primary А<->Е / consonant+А form."""
    v = genitive_variants(norm)
    return v[0] if v else None


@dataclass(frozen=True)
class House:
    """A parsed house number: numeric part + normalized Cyrillic suffix."""

    num: int | None
    suffix: str  # uppercase Cyrillic, '' if none
    raw: str

    def key(self) -> tuple[int | None, str]:
        return (self.num, self.suffix)


_HOUSE_RE = re.compile(r"^\s*(\d+)\s*[-/]?\s*([^\s].*?)?\s*$")


def normalize_house(s: str) -> House:
    """Parse a single house-number token into (num, suffix).

    Examples: '190' -> (190, ''); '190Б'/'190-Б'/'190B' -> (190, 'Б');
    '0-ББ' -> (0, 'ББ'). Suffix letters are folded to uppercase Cyrillic.
    A token with no leading digits yields num=None (caller flags it).
    """
    raw = nfc(s).strip()
    m = re.match(r"^(\d+)(.*)$", raw)
    if not m:
        return House(num=None, suffix=normalize_suffix(raw), raw=raw)
    num = int(m.group(1))
    suffix = normalize_suffix(m.group(2))
    return House(num=num, suffix=suffix, raw=raw)


def normalize_suffix(s: str) -> str:
    """Strip separators and fold a house-number suffix to uppercase Cyrillic."""
    s = nfc(s).strip().strip("-/ ").strip()
    if not s:
        return ""
    if _HAS_LATIN_RE.search(s):
        s = lat_to_cyr(s)
    return s.upper()
