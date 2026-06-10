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
    s = _WS_RE.sub(" ", s).strip()
    return s


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
