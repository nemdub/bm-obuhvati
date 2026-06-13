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
    # Keep a separator: the register often glues the period to the name ("ДР.МЛАДЕНА"), and
    # without the space the `\bДР\b` -> ДОКТОРА pass below can't fire, so it would normalize to
    # "ДРМЛАДЕНА" and never converge with a doc's spaced "Др Младена" -> "ДОКТОРА МЛАДЕНА".
    "ДР.": "ДР ",
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


# Spelled-out Serbian ordinals -> Arabic. The same street appears as "Краља Петра
# Првог", "Краља Петра I" and "Краља Петра 1" — the register itself uses all three.
# Roman numerals already fold to Arabic above; fold the spelled-out ordinal words too
# so every form converges (applied symmetrically to doc and register). Matched only as
# whole adjective-declined words — a trailing inflection is REQUIRED — so cardinals
# ("ПЕТ", "СЕДАМ") and unrelated words ("ДРУГОВИ", "ОСМАНЛИЈА", "ПРВЕНСТВА") are left
# untouched. Stems are tried longest-first so "ПЕТНАЕСТ" beats "ПЕТ".
_ORDINAL_STEMS = {
    "ПРВ": 1, "ДРУГ": 2, "ТРЕЋ": 3, "ЧЕТВРТ": 4, "ПЕТ": 5,
    "ШЕСТ": 6, "СЕДМ": 7, "ОСМ": 8, "ДЕВЕТ": 9, "ДЕСЕТ": 10,
    "ЈЕДАНАЕСТ": 11, "ДВАНАЕСТ": 12, "ТРИНАЕСТ": 13, "ЧЕТРНАЕСТ": 14,
    "ПЕТНАЕСТ": 15, "ШЕСНАЕСТ": 16, "СЕДАМНАЕСТ": 17, "ОСАМНАЕСТ": 18,
    "ДЕВЕТНАЕСТ": 19, "ДВАДЕСЕТ": 20,
}
# Hard- and soft-adjective inflections (ОГ/ОГА/ОМ..., ЕГ/ЕГА/ЕМ... for soft "ТРЕЋИ"),
# longest first so e.g. "ОГА" is preferred over "ОГ". A non-empty ending is required.
_ORDINAL_RE = re.compile(
    r"\b(" + "|".join(sorted(_ORDINAL_STEMS, key=len, reverse=True)) + r")"
    r"(?:ОГА|ОМЕ|ОМУ|ЕГА|ЕМУ|ИМА|ОГ|ОМ|ЕГ|ЕМ|ИМ|ИХ|ОЈ|ЕЈ|И|А|О|Е|У)\b"
)


def _ordinal_to_arabic(m: re.Match) -> str:
    return str(_ORDINAL_STEMS[m.group(1)])
# Latin letters that may appear in suffixes coming from the register's Latin column.
_HAS_LATIN_RE = re.compile(r"[A-Za-zĐŽĆČŠđžćčš]")

# Latin/Cyrillic homoglyphs: source docs occasionally type a visually identical Latin
# letter inside an otherwise-Cyrillic word ("AПАТИН" with a Latin 'A'), so the word fails
# to match its all-Cyrillic register form (its home settlement then can't be resolved).
# Fold the Latin homoglyphs to Cyrillic, but ONLY in words that mix both scripts — pure-
# Latin Roman numerals (VIII, XII) and the register's Latin house-letters stay untouched.
_HOMOGLYPH_TRANS = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "J": "Ј", "K": "К",
    "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
})
_LATIN_LETTER_RE = re.compile(r"[A-Z]")
_CYR_RANGE_RE = re.compile(r"[Ѐ-ӿ]")
_WORD_RE = re.compile(r"\S+")


def _fold_homoglyphs(s: str) -> str:
    def fix(m: re.Match) -> str:
        w = m.group(0)
        if _LATIN_LETTER_RE.search(w) and _CYR_RANGE_RE.search(w):
            return w.translate(_HOMOGLYPH_TRANS)
        return w

    return _WORD_RE.sub(fix, s)


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
    s = _fold_homoglyphs(s)
    s = _DR_RE.sub("ДОКТОРА", s)
    s = _ROMAN_RE.sub(_roman_to_arabic, s)
    s = _ORDINAL_RE.sub(_ordinal_to_arabic, s)
    s = _WS_RE.sub(" ", s).strip()
    return s


# House-suffix ordering (azbuka) for suffix-bounded ranges: '' < А < Б < ... < Ш.
SUFFIX_AZBUKA = "АБВГДЂЕЖЗИЈКЛЉМНЊОПРСТЋУФХЦЧЏШ"


def suffix_rank(s: str) -> tuple:
    """Sort key for a normalized house suffix; '' sorts before any letter."""
    return tuple(
        SUFFIX_AZBUKA.index(ch) if ch in SUFFIX_AZBUKA else 100 + ord(ch) for ch in s
    )


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
