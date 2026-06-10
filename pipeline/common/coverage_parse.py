"""Coverage-text parser.

Turns a polling station's free-text coverage cell into structured segments, one per
street clause. Two dialects:

  structured (Ada):  "Насеље: Ада Улица: 8. Март бројеви 1, 2, 3; Улица: ... бројеви ..."
  compact (Bor/Subotica): "Алеја маршала Тита 2-10, Антонија Хаџића, Цара Лазара 1-23 и 2-22А и Целовечка"

The compact dialect is heuristic (commas separate both streets and numbers; "и" joins
ranges or trailing streets) and is the #1 source of parse error — every compact segment
is later flagged for human review. Numbers are NOT expanded here; ranges resolve against
the real register in stage04.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .normalize import normalize_suffix
from .transliterate import nfc

_WS = re.compile(r"\s+")
# Range: lo-hi with an optional suffix on the upper bound ('1-23ц', '2-14А', '14-16/1',
# '2-20-А'). The upper bound must be numeric so '12-А' stays a single (12 suffix А).
_RANGE = re.compile(r"^(\d+)\s*[-–]\s*(\d+)\s*[-–/]?\s*([0-9А-Яа-яЂ-џA-Za-z]*)$")
_SINGLE = re.compile(r"^(\d+)\s*[-–]?\s*([0-9А-Яа-яЂ-џA-Za-z/]*)$")
_ORDINAL = re.compile(r"^\d+\.$")


def collapse(s: str) -> str:
    return _WS.sub(" ", nfc(s)).strip()


@dataclass
class Segment:
    settlement_raw: str
    street_raw: str
    kind: str  # street_numbers | whole_street | named_block | unknown
    intervals: list[list[int]] = field(default_factory=list)      # [[lo, hi], ...]
    singles: list[list] = field(default_factory=list)             # [[num, "suffix"], ...]
    unknown_tokens: list[str] = field(default_factory=list)
    whole: bool = False
    dialect: str = "compact"

    def to_parsed(self) -> dict:
        return {
            "intervals": self.intervals,
            "singles": self.singles,
            "whole": self.whole,
            "unknown_tokens": self.unknown_tokens,
        }


def interval_parity(lo: int, hi: int) -> str:
    """Serbian streets number odd/even on opposite sides. A range whose bounds are both
    odd (17-23) or both even (22-30) implies that side only; mixed bounds (1-20) cover
    both. Returns 'odd' | 'even' | 'all'."""
    if lo % 2 == 1 and hi % 2 == 1:
        return "odd"
    if lo % 2 == 0 and hi % 2 == 0:
        return "even"
    return "all"


# Block designations from housing estates: a short letter prefix + number ("А-21",
# "Т-8-Т-10", "Е1-Е-7/I"). These are an addressing system separate from the register's
# plain house numbers and cannot be auto-mapped — they are kept as unknown_tokens (->
# review) but must NOT be glued into the street name.
_BLOCK_RE = re.compile(r"^[A-Za-zА-Яа-яЂ-џ]{1,2}-?\d\S*$")


def is_block_token(w: str) -> bool:
    return bool(_BLOCK_RE.match(w.strip(".,;")))


def is_house_token(w: str) -> bool:
    """A house-number token starts with a digit and is not an ordinal ('20.', '8.')."""
    if not w or not w[0].isdigit():
        return False
    if _ORDINAL.match(w):
        return False
    return True


def is_number_side(w: str) -> bool:
    """Token belongs to the number side of a street clause (house number or block tag)."""
    return is_house_token(w) or is_block_token(w)


def parse_number_token(tok: str, seg: Segment) -> None:
    """Classify one number token into the segment (range / single / unknown)."""
    t = tok.strip().strip(".,;").strip()
    if not t:
        return
    m = _RANGE.match(t)
    if m and m.group(2):
        lo, hi = int(m.group(1)), int(m.group(2))
        # Store implied parity as a third element so it can be reviewed/overridden later.
        seg.intervals.append([lo, hi, interval_parity(lo, hi)])
        return
    m = _SINGLE.match(t)
    if m:
        seg.singles.append([int(m.group(1)), normalize_suffix(m.group(2) or "")])
        return
    seg.unknown_tokens.append(t)


def _add_numbers(seg: Segment, words: list[str]) -> None:
    for w in words:
        if w == "и":
            continue
        parse_number_token(w, seg)
    if seg.intervals or seg.singles:
        seg.whole = False


def _split_on_connector(words: list[str]) -> list[list[str]]:
    """Split a word list on standalone 'и' into sub-clauses."""
    out: list[list[str]] = []
    cur: list[str] = []
    for w in words:
        if w == "и":
            if cur:
                out.append(cur)
                cur = []
        else:
            cur.append(w)
    if cur:
        out.append(cur)
    return out


def _new_segment(settlement: str, name_words: list[str], num_words: list[str]) -> Segment:
    street = " ".join(name_words).strip()
    kind = "named_block" if street.upper().startswith("БЛОК") else "street_numbers"
    seg = Segment(settlement_raw=settlement, street_raw=street, kind=kind, dialect="compact")
    _add_numbers(seg, num_words)
    if not (seg.intervals or seg.singles or seg.unknown_tokens):
        seg.whole = True
        seg.kind = "whole_street" if kind != "named_block" else kind
    return seg


# ── Compact dialect ─────────────────────────────────────────────────────────
def parse_compact(text: str, settlement: str = "") -> list[Segment]:
    text = collapse(text).rstrip(". ")
    fragments = [f.strip() for f in text.split(",") if f.strip()]
    segments: list[Segment] = []
    last_street: Segment | None = None

    for frag in fragments:
        words = frag.split()
        # Leading continuation numbers belong to the previous street.
        i = 0
        lead: list[str] = []
        while i < len(words):
            w = words[i]
            if is_number_side(w):
                lead.append(w)
                i += 1
            elif w == "и" and i + 1 < len(words) and is_number_side(words[i + 1]):
                i += 1
            else:
                break
        if lead and last_street is not None:
            _add_numbers(last_street, lead)

        rest = words[i:]
        while rest and rest[0] == "и":
            rest = rest[1:]
        if not rest:
            continue

        for piece in _split_on_connector(rest):
            j = 0
            while j < len(piece) and not is_number_side(piece[j]):
                j += 1
            name_words, num_words = piece[:j], piece[j:]
            if not name_words:
                if last_street is not None:
                    _add_numbers(last_street, num_words)
                continue
            seg = _new_segment(settlement, name_words, num_words)
            segments.append(seg)
            last_street = seg
    return segments


# ── Structured dialect ──────────────────────────────────────────────────────
_NASELJE = re.compile(r"Насеље\s*:?\s*", re.IGNORECASE)
_ULICA = re.compile(r"Улица\s*:?\s*", re.IGNORECASE)
_BROJEVI = re.compile(r"\bброј\w*\b", re.IGNORECASE)


def parse_structured(text: str) -> list[Segment]:
    text = collapse(text)
    segments: list[Segment] = []
    settlement = ""
    # Chunks are street clauses separated by ';'. A chunk may carry a 'Насеље:' prefix.
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        nm = _NASELJE.search(chunk)
        um = _ULICA.search(chunk)
        if nm and (not um or nm.start() < um.start()):
            after = chunk[nm.end():]
            settlement = (after[: um.start() - nm.end()] if um else after).strip()
            if not um:
                continue
            chunk = chunk[um.start():]
            um = _ULICA.search(chunk)
        if not um:
            continue
        body = chunk[um.end():].strip()
        bm = _BROJEVI.search(body)
        if bm:
            street = body[: bm.start()].strip()
            numbers = body[bm.end():].strip()
            seg = Segment(settlement_raw=settlement, street_raw=street,
                          kind="street_numbers", dialect="structured")
            for tok in re.split(r"[,\s]+и\s+|,", numbers):
                parse_number_token(tok, seg)
            if not (seg.intervals or seg.singles or seg.unknown_tokens):
                seg.whole = True
                seg.kind = "whole_street"
        else:
            seg = Segment(settlement_raw=settlement, street_raw=body.strip(),
                          kind="whole_street", whole=True, dialect="structured")
        if seg.street_raw:
            segments.append(seg)
    return segments


def parse_coverage(text: str) -> list[Segment]:
    """Detect dialect and parse the coverage cell into segments."""
    if not text or not text.strip():
        return []
    is_structured = bool(_ULICA.search(text) and _BROJEVI.search(text))
    return parse_structured(text) if is_structured else parse_compact(text)
