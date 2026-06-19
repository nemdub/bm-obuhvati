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

from .normalize import normalize_street, normalize_suffix
from .transliterate import nfc

_WS = re.compile(r"\s+")
# Range: lo-hi with optional suffixes on either bound ('1-23ц', '12а-16', '2-20-А',
# '14-16/1'). The upper bound must contain digits so '12-А' stays a single (12 suffix А).
# Suffixed bounds are stored as 5-element intervals [lo, hi, parity, lo_sfx, hi_sfx] and
# matched inclusively up to/from the suffix (suffix order = azbuka).
_RANGE = re.compile(r"^(\d+)([А-Яа-яЂ-џA-Za-z]*)\s*[-–]\s*(\d+)\s*[-–/]?\s*([0-9А-Яа-яЂ-џA-Za-z]*)$")
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
    # "бб" (bez broja / without number): the street's houses that carry no assigned number.
    # Additive — a segment may have ranges/singles AND bez_broja.
    bez_broja: bool = False
    dialect: str = "compact"

    def to_parsed(self) -> dict:
        return {
            "intervals": self.intervals,
            "singles": self.singles,
            "whole": self.whole,
            "bez_broja": self.bez_broja,
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


# "бб" (bez broja / "without number"): a marker, not a house number — tolerant of dots and
# case ('бб', 'ББ', 'бб.', 'б.б.') and the Latin form ('bb'). Kept on the number side so it
# leaves the street name and becomes a `bez_broja` flag instead of a phantom street.
_BB_RE = re.compile(r"^(?:бб|bb)$", re.IGNORECASE)


def is_bb_token(w: str) -> bool:
    return bool(_BB_RE.match(w.strip(".,;").replace(".", "")))


# "бр." / "број" / "броја" / "бројеви" — the label that introduces house numbers ("Нова 27
# бр. 5-9", "од броја 33 до 117"). Neither part of the street name nor a house number itself:
# it ends the name and is then dropped from the number side.
_BROJ_RE = re.compile(r"^(?:бр|број[А-Яа-яЂ-џ]*|broj[a-z]*)$", re.IGNORECASE)


def is_broj_token(w: str) -> bool:
    return bool(_BROJ_RE.match(w.strip(".,;").replace(".", "")))


# "од N до M", "од N до краја" — Serbian range grammar. "од" (from) starts a range and "до"
# (to) connects the bounds; "до краја" means "to the end of the street" (open-ended upper
# bound). NB: "до"/"До" is also a common toponym ("Добри До", "Милошев До"), so "до" is only
# ever a connector BETWEEN numbers (handled in _add_numbers) — in a street name it stays put.
# "до краја" upper bound: a sentinel above any real house number (register max is ~2159).
OPEN_END = 100000
_KRAJA = {"краја", "крај", "краj"}


def _word(w: str) -> str:
    return w.strip(".,;").lower()


def is_od_token(w: str) -> bool:
    """The range-start preposition 'од' (from) — ends a street name, dropped from numbers."""
    return _word(w) == "од"


# Side-of-street adjective forms ("парна страна", "парни бројеви", "на парној страни", …).
# Matched as an EXACT declined-form set, NOT a "парн…" prefix, so the register streets that
# start with the same stem (ПАРНИЦА, ПАРНИЧКА — the only two nationwide) are never mistaken
# for a side directive.
_PARNA_FORMS = {"парна", "парне", "парни", "парно", "парној", "парну",
                "парнога", "парном", "парним", "парних"}
_NEPARNA_FORMS = {"непарна", "непарне", "непарни", "непарно", "непарној", "непарну",
                  "непарнога", "непарном", "непарним", "непарних"}


def _side_parity(w: str) -> str | None:
    """Side-of-street indicator: 'парна/парној страна' -> even, 'непарна/непарној' -> odd."""
    wl = _word(w)
    if wl in _NEPARNA_FORMS:
        return "odd"
    if wl in _PARNA_FORMS:
        return "even"
    return None


def is_house_token(w: str) -> bool:
    """A house-number token starts with a digit and is not an ordinal ('20.', '8.')."""
    if not w or not w[0].isdigit():
        return False
    if _ORDINAL.match(w):
        return False
    return True


def is_number_side(w: str) -> bool:
    """Token belongs to the number side of a street clause (house number, block tag, or 'бб')."""
    return is_house_token(w) or is_block_token(w) or is_bb_token(w)


def parse_number_token(tok: str, seg: Segment) -> None:
    """Classify one number token into the segment (range / single / unknown)."""
    t = tok.strip().strip(".,;").strip()
    if not t:
        return
    if is_bb_token(t):
        seg.bez_broja = True
        return
    m = _RANGE.match(t)
    if m and m.group(3):
        lo, hi = int(m.group(1)), int(m.group(3))
        lo_sfx = normalize_suffix(m.group(2) or "")
        hi_sfx = normalize_suffix(m.group(4) or "")
        # Implied parity rides as the third element so it can be reviewed/overridden later.
        iv = [lo, hi, interval_parity(lo, hi)]
        if lo_sfx or hi_sfx:
            iv += [lo_sfx, hi_sfx]
        seg.intervals.append(iv)
        return
    m = _SINGLE.match(t)
    if m:
        seg.singles.append([int(m.group(1)), normalize_suffix(m.group(2) or "")])
        return
    seg.unknown_tokens.append(t)


def _add_numbers(seg: Segment, words: list[str]) -> None:
    i, n = 0, len(words)
    # A side word ("парна"/"непарни"/...) qualifies the parity of a nearby range. It may come
    # BEFORE the range ("непарни од 1 до 9"), AFTER it ("2-100 на парној"), or stand ALONE
    # ("Белодримска непарна страна", "Љубе Нешића парни бројеви") — meaning the whole side of
    # the street. We remember it as `pending_side` and resolve it against the range it touches.
    pending_side: str | None = None
    base_iv, base_sg, base_uk = len(seg.intervals), len(seg.singles), len(seg.unknown_tokens)
    while i < n:
        w = words[i]
        wl = _word(w)
        # Range-grammar fillers, dropped: list "и", "од" (from), "бр./број(а)" (label),
        # "па" (and onwards), "на"/"страна/страни" (side-of-street phrasing).
        if wl in ("и", "од", "па", "на", "страна", "страни", "стране", "страну") or is_broj_token(w):
            i += 1
            continue
        # Side indicator ("парн…"/"непарн…"): remember it; applied to the range it qualifies.
        side = _side_parity(w)
        if side is not None:
            pending_side = side
            i += 1
            continue
        # "N до краја" / "N до M": a "до"-connected range (only here is "до" a connector, not
        # the toponym "До"). Skip any "бр."/"па" between the bound and the connector.
        if is_house_token(w):
            k = i + 1
            while k < n and (is_broj_token(words[k]) or _word(words[k]) == "па"):
                k += 1
            if k < n and _word(words[k]) == "до":
                k += 1
                while k < n and is_broj_token(words[k]):
                    k += 1
                lo = int(re.match(r"\d+", w).group())
                if k < n and _word(words[k]) in _KRAJA:
                    seg.intervals.append([lo, OPEN_END, "odd" if lo % 2 else "even"])
                    i = k + 1
                    pending_side = _apply_pending_side(seg, pending_side)
                    continue
                if k < n and is_house_token(words[k]):
                    hi = int(re.match(r"\d+", words[k]).group())
                    seg.intervals.append([lo, hi, interval_parity(lo, hi)])
                    i = k + 1
                    pending_side = _apply_pending_side(seg, pending_side)
                    continue
        before = len(seg.intervals)
        parse_number_token(w, seg)
        if pending_side and len(seg.intervals) > before:
            pending_side = _apply_pending_side(seg, pending_side)
        i += 1
    # A side word left unconsumed: apply it to the range it touched, or — if this call added no
    # numbers at all — treat it as a whole-side claim ("непарна страна" => all odd / all even).
    if pending_side is not None:
        if len(seg.intervals) > base_iv:
            seg.intervals[-1][2] = pending_side
        elif len(seg.singles) == base_sg and len(seg.unknown_tokens) == base_uk:
            seg.intervals.append([1 if pending_side == "odd" else 2, OPEN_END, pending_side])
    if seg.intervals or seg.singles:
        seg.whole = False


def _apply_pending_side(seg: Segment, pending_side: str | None) -> None:
    """Apply a pending side-of-street parity to the interval just appended, then clear it."""
    if pending_side and seg.intervals:
        seg.intervals[-1][2] = pending_side
    return None


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


def _merge_street_connectors(pieces: list[list[str]], is_street) -> list[list[str]]:
    """Re-join clauses split on 'и' when the joined name is a real register street.

    Some street names contain a literal "и" ("Зрињског и Франкопана", "Трг Јакаба и
    Комора"). `_split_on_connector` would split those into two phantom streets. When a
    register-membership predicate is supplied, a name-only clause is merged with the
    following clause iff "<clause> и <next-name>" normalizes to a known street in the
    station's municipality — so genuine list connectors ("Антонија Хаџића и Целовечка",
    two real streets) are left split while compound names are kept whole."""
    if is_street is None or len(pieces) < 2:
        return pieces
    out: list[list[str]] = []
    i = 0
    while i < len(pieces):
        cur = pieces[i]
        # Only a clause with no number tokens can be the left side of a compound name
        # (a numbered claim ends the street name before the 'и').
        while i + 1 < len(pieces) and all(not is_number_side(w) for w in cur):
            nxt = pieces[i + 1]
            prefix: list[str] = []
            for w in nxt:
                # The street name ends at the first number or parenthetical alternate name
                # ("Трг Јакаба и Комора (Трг октобарске револуције) 28-30").
                if is_number_side(w) or w.startswith("("):
                    break
                prefix.append(w)
            if not prefix or not is_street(normalize_street(" ".join(cur + ["и"] + prefix))):
                break
            cur = cur + ["и"] + nxt
            i += 1
        out.append(cur)
        i += 1
    return out


def _new_segment(settlement: str, name_words: list[str], num_words: list[str]) -> Segment:
    street = " ".join(name_words).strip()
    kind = "named_block" if street.upper().startswith("БЛОК") else "street_numbers"
    seg = Segment(settlement_raw=settlement, street_raw=street, kind=kind, dialect="compact")
    _add_numbers(seg, num_words)
    if not (seg.intervals or seg.singles or seg.unknown_tokens or seg.bez_broja):
        seg.whole = True
        seg.kind = "whole_street" if kind != "named_block" else kind
    return seg


# ── Compact dialect ─────────────────────────────────────────────────────────
def parse_compact(text: str, settlement: str = "", is_street=None) -> list[Segment]:
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

        for piece in _merge_street_connectors(_split_on_connector(rest), is_street):
            j = 0
            while j < len(piece):
                # The "бр."/"број" label and the "од" (from) preposition both end the street
                # name and introduce house numbers ("Нова 27 бр. 5-9", "Стевана Чоловића од
                # 1-17"). "од" is unambiguous here (the toponym is "До", not "од").
                if is_broj_token(piece[j]) or is_od_token(piece[j]):
                    break
                # A parity word ends the street name and begins a side directive ("Белодримска
                # непарна страна", "Љубе Нешића парни бројеви", "Гаврилова непарни од 1 до 9").
                # At j == 0 the name is empty, so the directive continues the previous street
                # ("Краља Петра Првог 0 и непарни бројеви"). _side_parity only matches the exact
                # adjective forms, so register streets like ПАРНИЧКА are never split here.
                if _side_parity(piece[j]) is not None:
                    break
                if not is_number_side(piece[j]):
                    j += 1
                    continue
                # "Угриновачки пут 1 део": an integer followed by 'део' is part of the
                # street NAME (register has "... N ДЕО" streets), not a house number.
                nxt = piece[j + 1].strip(".,;").lower() if j + 1 < len(piece) else ""
                if is_house_token(piece[j]) and nxt == "део":
                    j += 2
                    continue
                # "Блок 112 С-1": a number right after 'Блок' is the BLOCK's name
                # (register street "БЛОК 112"), not a house number.
                prev = piece[j - 1].strip(".,;").upper() if j > 0 else ""
                if is_house_token(piece[j]) and prev in ("БЛОК", "БЛОКА"):
                    j += 1
                    continue
                # "Нова 4", "Нова 21": a trailing number that, together with the name so
                # far, is itself a register street is part of the NAME, not a house number.
                # Without this each "Нова N" parses as house N of a single "Нова" street and
                # they collapse into one segment. Register-driven, like the 'и' merge above.
                # Requires a name stem before the number (j > 0) so a bare number continuing
                # the previous street ("Стројковце 0 и 1") is never promoted to a street even
                # when "1" happens to be a register street name elsewhere in the muni.
                if (j > 0 and is_street is not None and is_house_token(piece[j])
                        and is_street(normalize_street(" ".join(piece[: j + 1])))):
                    j += 1
                    continue
                break
            name_words, num_words = piece[:j], piece[j:]
            # Drop a trailing separator dash, but ONLY before a parity directive
            # ("Бањска - непарна страна" => name "Бањска"). A dash that is part of the street
            # name ("Потес Јездинско поље - 1 нова") is followed by numbers, not a side word,
            # and is left intact.
            if num_words and _side_parity(num_words[0]) is not None:
                while name_words and name_words[-1] in ("-", "–"):
                    name_words.pop()
            if not name_words:
                if last_street is not None:
                    _add_numbers(last_street, num_words)
                continue
            street = " ".join(name_words).strip()
            # Documents often repeat the street per building ("Блок 112 С-1, Блок 112
            # С-2, ..."): merge into the existing same-named segment, one card per street.
            existing = next((x for x in segments if x.street_raw == street
                             and x.settlement_raw == settlement), None)
            if existing is not None:
                _add_numbers(existing, num_words)
                last_street = existing
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
            # No 'бројеви': a whole street, but a trailing "бб" is the bez-broja marker,
            # not part of the name ("Улица: Омладинских бригада бб").
            name_words = body.split()
            bez = False
            while name_words and is_bb_token(name_words[-1]):
                name_words.pop()
                bez = True
            seg = Segment(settlement_raw=settlement, street_raw=" ".join(name_words).strip(),
                          kind="whole_street", whole=not bez, bez_broja=bez, dialect="structured")
        if seg.street_raw:
            segments.append(seg)
    return segments


# "Nth part" street names ("Угриновачки пут 1 део"): documents sometimes glue the part
# word to the following house number ("део13") — split so tokenization sees them apart.
_DEO_GLUE = re.compile(r"(?i)\b(део)(\d)")
# A space around the range dash ("2- 100", "2 - 100") splits the range into two tokens —
# collapse it so the bound stays one token ("2-100"). Only between digits, so block tags
# ("С-1") and suffix tails are untouched.
_DASH_SPACE = re.compile(r"(\d)\s*[-–]\s*(\d)")
# Ordinal glued to the following word ("7.јула", "1.маја", "10.октобра") — split so "7." is
# seen as an ordinal (street-name part) instead of a house number "7" + junk.
_ORDINAL_GLUE = re.compile(r"(\d+\.)([А-Яа-яЂ-џA-Za-z])")
# A dash used in place of the "од … до" range form glues the lower bound onto "до": "98-до
# краја" means "од 98 до краја" (98 to the end of the street). Split the dash off so the
# "N до краја" / "N до M" grammar (_add_numbers, 2.12) sees it. Only when "до" follows a
# digit-dash — a plain "N-M" range (digit-dash-digit) is untouched.
_NUM_DO_DASH = re.compile(r"(\d)\s*[-–]\s*(до)\b", re.IGNORECASE)
# Prose list-introducer: some docs (Беочин) prefix the street list with a sentence ending
# "...у улици:" / "...у улицама:" ("voters residing in MZ … in the street(s):"). Strip up to
# and including it so the sentence isn't glued onto the first street. Nationwide this
# colon-terminated marker occurs only in this preamble; the structured 'Улица:' label is
# never preceded by "у ", so structured docs are untouched.
_LIST_PREAMBLE_RE = re.compile(r"^.*?\bу\s+улиц(?:и|ама)\s*:\s*", re.IGNORECASE | re.DOTALL)


def parse_coverage(text: str, is_street=None) -> list[Segment]:
    """Detect dialect and parse the coverage cell into segments.

    `is_street(normalized_name) -> bool`, when supplied, lets the compact parser keep
    street names that contain a literal "и" ("Зрињског и Франкопана") whole instead of
    splitting them on the connector (see `_merge_street_connectors`)."""
    if not text or not text.strip():
        return []
    text = _LIST_PREAMBLE_RE.sub("", text)
    text = _DEO_GLUE.sub(r"\1 \2", text)
    text = _NUM_DO_DASH.sub(r"\1 \2", text)
    text = _DASH_SPACE.sub(r"\1-\2", text)
    text = _ORDINAL_GLUE.sub(r"\1 \2", text)
    is_structured = bool(_ULICA.search(text) and _BROJEVI.search(text))
    return parse_structured(text) if is_structured else parse_compact(text, is_street=is_street)
