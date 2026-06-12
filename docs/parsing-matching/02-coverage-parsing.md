# 2. Coverage‑text parsing

Code: `pipeline/common/coverage_parse.py` (driven by `stage03_parse_coverage.py`)

Turns a polling station's free‑text coverage cell into a list of **segments**, one per
street clause. Numbers are **not** expanded here — ranges are resolved against the real
register in stage04. Each segment carries the *raw* street name; normalization and street
resolution happen later.

> Test target: `parse_coverage`, `parse_compact`, `parse_structured`, `parse_number_token`,
> `interval_parity`, `is_block_token`, `is_bb_token`, `is_house_token`, `is_number_side`,
> `_merge_street_connectors`, `_split_on_connector`.

## 2.0 The `Segment` shape

```python
Segment(
  settlement_raw: str,         # "Насеље:" label if structured dialect, else ""
  street_raw:     str,         # raw street name as printed
  kind:           str,         # street_numbers | whole_street | named_block | unknown
  intervals:      list,        # [[lo, hi, parity], ...] or [[lo, hi, parity, loSfx, hiSfx], ...]
  singles:        list,        # [[num, "SUFFIX"], ...]
  unknown_tokens: list[str],   # tokens that couldn't be classified -> review
  whole:          bool,        # claims the entire street
  bez_broja:      bool,        # claims the street's no-number (house_num IS NULL) houses
  dialect:        str,         # "compact" | "structured"
)
```

`to_parsed()` serializes `intervals / singles / whole / bez_broja / unknown_tokens` to JSON
(`parsed_json`), the contract consumed by stage04 and the Worker.

In stage03 each segment gets `id = station_id * 1000 + index`. **Segment ids are positional**
— this is why merges and drops that change index counts must happen at parse time, not later
(reviewer overrides are keyed to the id).

---

## 2.1 Dialect detection (`parse_coverage`)

**Rule:** the **structured** dialect is used iff the text contains both an `Улица` label and
a `број…` token (`_ULICA.search(text) and _BROJEVI.search(text)`). Otherwise **compact**.

Before detection, glued `део` is split (`_DEO_GLUE`: `део13` → `део 13`, see 2.7).

`is_street` (a register‑membership predicate) is threaded into the compact path only — used
by the compound‑`и` merge (2.8).

### Examples

- `Насеље: Ада Улица: 8. Март бројеви 1, 2, 3` → **structured**.
- `Алеја маршала Тита 2-10, Антонија Хаџића, Цара Лазара 1-23 и 2-22А` → **compact**.

---

## 2.2 Number‑token classification (`parse_number_token`)

Classifies one number‑side token into the segment. After stripping `.,;` whitespace:

1. **`бб` token** (`is_bb_token`) → set `seg.bez_broja = True` (see 2.4).
2. **Range** (`_RANGE`, must have a hi digit group) → append interval (see 2.3).
3. **Single** (`_SINGLE`) → append `[num, normalize_suffix(rest)]`.
4. **Otherwise** → append raw token to `unknown_tokens` (→ review).

`_add_numbers` skips standalone `и`; once any interval/single is present it forces
`whole = False`.

### Token predicates

- **`is_house_token(w)`**: starts with a digit and is **not** an ordinal (`_ORDINAL = ^\d+\.$`).
  So `20.` and `8.` are **not** house tokens (they're list ordinals); `20` and `20а` are.
- **`is_block_token(w)`**: `_BLOCK_RE = ^[A-Za-zА-Яа-яЂ-џ]{1,2}-?\d\S*$` — a 1–2 letter prefix
  then a digit (`А-21`, `Т-8`, `Е1-Е-7/I`). See 2.5.
- **`is_bb_token(w)`**: `бб`/`ББ`/`бб.`/`б.б.`/Latin `bb`, dot‑ and case‑tolerant (dots are
  stripped before matching).
- **`is_number_side(w)`** = house OR block OR bb. This is the boundary between the street
  name and the number side of a clause.

---

## 2.3 Ranges, parity, and suffix bounds (`_RANGE`, `interval_parity`)

A range `lo-hi` (`_RANGE`) parses to `[lo, hi, parity]`, or `[lo, hi, parity, loSfx, hiSfx]`
when either bound carries a suffix.

- The **upper bound must contain digits** — `12-А` is NOT a range, it's a single `12` suffix
  `А` (handled by `_SINGLE`).
- Suffixes on bounds are normalized (`normalize_suffix`): `1-23ц` → `[1, 23, 'odd', '', 'Ц']`.
- `12а-16` → `[12, 16, 'even', 'А', '']` (previously this was an `unknown_token`).
- Separators tolerated between bounds: `-`, `–`, `/` (`2-20-А`, `14-16/1`).

### Parity (`interval_parity(lo, hi)`)

Serbian streets number odd on one side, even on the other.

| bounds | parity | meaning |
|--------|--------|---------|
| both odd (`17-23`) | `odd` | odd side only |
| both even (`22-30`) | `even` | even side only |
| mixed (`1-20`) | `all` | both sides |

Parity rides as the 3rd interval element so it can be reviewed/overridden later. It is
**inferred, not asserted** — stage04 validates it against sibling coverage (see
[06](06-claim-resolution.md) §parity).

---

## 2.4 `бб` / bez broja (no house number)

**Rule:** `бб` ("bez broja", without number) is a *marker*, not a house number. It is kept
on the number side (so it leaves the street name) and sets `seg.bez_broja = True`. It is
**additive** — a segment can have ranges/singles AND `bez_broja`.

A `бб`‑only clause does **not** become `whole=True`: `_new_segment`'s whole‑guard counts
`bez_broja` as content (`not (intervals or singles or unknown_tokens or bez_broja)`), and in
the structured no‑`бројеви` branch a trailing `бб` is popped off the name and sets
`whole=False, bez_broja=True`.

### Examples

| Input | Result |
|-------|--------|
| `Омладинских бригада бб` | street `Омладинских бригада`, `bez_broja=True`, `whole=False` |
| `Бул. Михаила Пупина 2-6, 3-13 и бб` | intervals `[2,6],[3,13]` **and** `bez_broja=True` |
| `Улица: Омладинских бригада бб` (structured, no `бројеви`) | street `Омладинских бригада`, `bez_broja=True` |

### Rationale

The register holds no‑number houses as `house_num IS NULL`. Before this flag, the parser
made `бб` a phantom whole‑street segment or glued it into the name (breaking the register
match), and nothing linked NULL‑house addresses. The flag is schemaless JSON (no migration).
A *plain* whole‑street claim also covers NULL houses (user decision); an explicit `бб`
outranks a generic whole there (see [06](06-claim-resolution.md)). Note: the local dev
subset has 0 NULL‑house addresses, so live NULL‑linking is unverified there.

---

## 2.5 Block tokens (`is_block_token`, `_BLOCK_RE`)

**Rule:** housing‑estate block designations (`А-21`, `Т-8-Т-10`, `Е1-Е-7/I`, `С-1`) are a
*different addressing system* from register house numbers and are **not auto‑mappable**.
They are kept on the **number side** as `unknown_tokens` (→ review), and must **not** be
glued into the street name.

### Examples

- `Цара Лазара А-21-А-24` → street `Цара Лазара`, `unknown_tokens=["А-21-А-24"]`.
- `... Т-8` → `unknown_tokens=["Т-8"]`.

### Rationale

Gluing block tags into the name produced bogus whole‑street claims and cross‑station
conflicts. Keeping them as unknown tokens added **+53k correct links** globally while
leaving the un‑mappable blocks honestly flagged.

### Exception — `Блок N` is part of the NAME

In the compact parser, a number immediately after `Блок`/`Блока` is the **block's name**,
not a house number: `Блок 112 С-1` → street `Блок 112` (register street `БЛОК 112`); the
`С-1` building label still rides as an unknown token. Such segments get `kind = "named_block"`
(`_new_segment`: name starts with `БЛОК`). See `parse_compact` `prev in ("БЛОК","БЛОКА")`.

---

## 2.6 Same‑street fragment merge (compact)

**Rule:** documents repeat a street once per building (`Блок 112 С-1, Блок 112 С-2, …`). The
compact parser merges a fragment into the existing same‑named segment (same `street_raw` AND
same `settlement_raw`) rather than creating a new card.

### Rationale

Removed **1,018 duplicate cards** nationwide; review flags −925. One card per street.

---

## 2.7 `N део` street names (`_DEO_GLUE`, the `део` guard)

**Rule:** an integer followed by `део` ("part") belongs to the street **name**, not the
number side — the register has streets named `... N ДЕО` (`Угриновачки пут 1 део`).

- `_DEO_GLUE` (in `parse_coverage`) splits glued forms: `део13` → `део 13`.
- In `parse_compact`, when scanning for the name/number boundary, a house token whose **next**
  token is `део` is pulled back into the name (`j += 2`).

### Examples

- `Угриновачки пут 1 део` → street `Угриновачки пут 1 део` (no house numbers).
- `Угриновачки пут 1 део 13` → street `Угриновачки пут 1 део`, single `13`.

Register convention: **part 1 is the plain base name**; parts start at 2. Resolution of
`... 1 ДЕО` → base name happens in stage04 (see [05](05-street-resolution.md)).

---

## 2.8 Compound `и` street names (`_split_on_connector`, `_merge_street_connectors`)

The compact parser splits clauses on a standalone `и`. But some street **names** contain a
literal `и` (`Зрињског и Франкопана`, `Трг Јакаба и Комора`, `Ћирила и Методија`,
`Маркса и Енгелса`, `Козме и Дамјана`, `Апостола Петра и Павла`). Naive splitting tears them
into two phantom whole‑street segments.

**Rule (register‑driven):** after splitting on `и`, re‑join a *name‑only* clause with the
next clause **iff** `"<clause> и <next-name-prefix>"` normalizes to a real register street in
the station's municipality (`is_street` predicate). Otherwise keep them split — genuine list
connectors (`Антонија Хаџића и Целовечка`, two real streets) stay separate.

Constraints (`_merge_street_connectors`):
- Only a clause with **no number tokens** can be the left side of a compound name.
- The next clause's name prefix ends at the **first number or `(`** — so the old‑name
  restatement form `Трг Јакаба и Комора (Трг октобарске револуције) 28-30` merges the name
  whole, leaving the parenthetical and numbers for later handling.
- Merging runs **within a comma fragment** only — never glues across commas.

### Examples

| Input (one fragment) | register has `ЗРИЊСКОГ И ФРАНКОПАНА`? | Result |
|----------------------|----------------------------------------|--------|
| `Зрињског и Франкопана` | yes | one segment, street `Зрињског и Франкопана` |
| `Антонија Хаџића и Целовечка` | no | two segments (`Антонија Хаџића`, `Целовечка`) |
| `Трг Јакаба и Комора (Трг ...) 28-30` | yes | one segment, name kept whole |

### Rationale

`X и Y` is genuinely ambiguous (Serbian lists also end "…A и B"), so disambiguation must be
register‑driven. Done at **parse time** because segment ids are positional — merging later
would shift indices and orphan reviewer overrides. Nationwide: 98 segments / 45 distinct
compound names kept whole; each was 2 phantom review items → 1 correct match.
`parse_structured` is unaffected (the street name precedes `бројеви`).

> **Pipeline note:** stage03 builds the per‑muni `is_street` set from register `name_norm`s
> containing the token `И`. After a parser change, re‑run stage03 + stage03b before
> `recompute.sh` (which starts at stage04).

---

## 2.9 Leading continuation numbers (compact)

**Rule:** in a comma fragment, leading number‑side tokens (before any street name) belong to
the **previous** street. `parse_compact` peels them off and appends to `last_street`. Also,
a fragment that is *only* numbers appends to `last_street`.

### Example

`Цара Лазара 1-23, 2-22А, Његошева` → `2-22А` continues `Цара Лазара`; `Његошева` is a new
whole‑street segment.

---

## 2.10 Whole‑street default (`_new_segment`)

**Rule:** a clause with a name but no intervals, no singles, no unknown_tokens, and no
`bez_broja` becomes `whole=True`, `kind = "whole_street"` (or stays `named_block` if the name
starts with `БЛОК`).

### Example

`Антонија Хаџића` (no numbers) → `whole_street`, `whole=True`.

---

## 2.11 Structured dialect (`parse_structured`)

For the `Насеље:`/`Улица:`/`бројеви` form (e.g. Ada). Rules:

- Chunks are split on `;`. A chunk may carry a `Насеље:` prefix that sets the current
  settlement (`settlement_raw`) for following chunks.
- Within a chunk, text after `Улица:` up to `број…` is the street; after `број…` are numbers.
- Numbers are split on `,` and `… и …` and classified via `parse_number_token`.
- **No `бројеви`** → whole street; a trailing `бб` is stripped and sets `bez_broja`.
- Empty street names are dropped.

### Example

`Насеље: Ада Улица: 8. Март бројеви 1, 2, 3` → settlement `Ада`, street `8. Март`,
singles `[1],[2],[3]`.
</content>
