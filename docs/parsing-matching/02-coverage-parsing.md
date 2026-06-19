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

`_add_numbers` is a small grammar over the number side, not a plain token loop. It drops
filler/label words, recognizes `од … до …` ranges, and applies side‑of‑street parity — see
2.12. Once any interval/single is present it forces `whole = False`.

### Token predicates

- **`is_house_token(w)`**: starts with a digit and is **not** an ordinal (`_ORDINAL = ^\d+\.$`).
  So `20.` and `8.` are **not** house tokens (they're list ordinals); `20` and `20а` are.
- **`is_block_token(w)`**: `_BLOCK_RE = ^[A-Za-zА-Яа-яЂ-џ]{1,2}-?\d\S*$` — a 1–2 letter prefix
  then a digit (`А-21`, `Т-8`, `Е1-Е-7/I`). See 2.5.
- **`is_bb_token(w)`**: `бб`/`ББ`/`бб.`/`б.б.`/Latin `bb`, dot‑ and case‑tolerant (dots are
  stripped before matching).
- **`is_broj_token(w)`**: the house‑number label `бр`/`бр.`/`број`/`броја`/`бројеви`/Latin
  `broj…` (`_BROJ_RE`, dot‑ and case‑tolerant). Neither name nor number — see 2.13.
- **`is_od_token(w)`**: the range‑start preposition `од` ("from"). Ends a street name, dropped
  from the number side — see 2.12.
- **`is_number_side(w)`** = house OR block OR bb. This is the boundary between the street
  name and the number side of a clause. (`бр.`/`од` are handled separately as name‑enders, not
  via `is_number_side`.)

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
> that **either contain the token `И`** (this rule) **or contain a digit** (the numbered‑name
> rule, 2.14). The same predicate serves both. After a parser change, re‑run stage03 +
> stage03b before `recompute.sh` (which starts at stage04).

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

---

## 2.12 Number‑side grammar: `од … до …`, `до краја`, side parity, fillers (`_add_numbers`)

The compact number side is not just a list of tokens — it is a small grammar (`_add_numbers`)
over fillers, the Serbian `од … до …` range form, and side‑of‑street phrasing.

### Dropped filler / label words

These are skipped (consumed, no effect on the parse):

- `и` (list connector), `од` (range‑start "from"),
- `бр.`/`број`/`броја`/`бројеви` (`is_broj_token`, the number label),
- `па` ("and onwards"), `на` and `страна`/`страни`/`стране`/`страну` (side‑of‑street phrasing).

### `од N до M` / `од N до краја` ranges

**Rule:** a house number followed by `до` ("to") forms a range. `до` is treated as a
connector **only between numbers** here — in a street name it stays put (the toponym is `До`,
e.g. `Добри До`, `Милошев До`). `бр.`/`па` between the bound and `до` (and after it) are
skipped.

- `од N до M` → `[N, M, interval_parity(N, M)]`.
- `од N до краја` ("to the end of the street") → open‑ended upper bound
  `[N, OPEN_END, parity]` where **`OPEN_END = 100000`** (a sentinel above any real house
  number; register max ≈ 2159) and parity follows N's own parity.
- `од броја N до M` works (the `броја` label is skipped).

A dash standing in for the `од … до` form is normalised away before parsing: **`98-до краја`**
(`= од 98 до краја`) is split by `_NUM_DO_DASH` (2.15) into `98 до краја`, so the rule above
applies and yields `[98, 100000, 'even']`. A plain digit‑to‑digit range (`2-44`) is untouched.

### Side‑of‑street parity (`_side_parity`)

**Rule:** a parity word — `парна`/`парни`/`парној`/… → `even`, `непарна`/`непарни`/… → `odd` —
is a side‑of‑street directive. It is tracked as a **pending side** and resolved against the
range it touches, in any of three positions:

1. **After** a range — overrides its parity: `2-100 на парној страни` → `[2, 100, 'even']`.
2. **Before** a range — sets the new range's parity: `непарни од 1 до 9` → `[1, 9, 'odd']`.
3. **Standalone** (no numbers at all) — claims the **whole side** of the street:
   `непарна страна` / `непарни бројеви` → `[1, 100000, 'odd']`;
   `парна страна` / `парни бројеви` → `[2, 100000, 'even']`.

The parity word also **ends the street name** in `parse_compact` (a name stem before it is the
street). A trailing separator dash is dropped (`Бањска - непарна страна` → name `Бањска`). When
the parity word starts a clause (`… 0 и непарни бројеви`), it continues the **previous** street.

`_side_parity` matches an **exact set** of declined forms, not a `парн…` prefix, so the only two
register streets that share the stem (`ПАРНИЦА`, `ПАРНИЧКА`) are never mistaken for a directive.

The pending side is scoped **per `_add_numbers` call**: a trailing side on one `и`‑clause cannot
corrupt an interval built in an earlier clause (`Светосавска ... 3-41 на непарној ... 12 на
парној` keeps `3-41` odd; the trailing `парној` qualifies house `12`, not the range).

### Examples

| Input | intervals |
|-------|-----------|
| `Стевана Чоловића од 1-17` | `[[1, 17, 'odd']]` (street `Стевана Чоловића`) |
| `Прва од 33 до 117` | `[[33, 117, 'odd']]` |
| `Прва од броја 33 до 117` | `[[33, 117, 'odd']]` |
| `Прва од 5 до краја` | `[[5, 100000, 'odd']]` |
| `Лазара Мићуновића 98-до краја` | `[[98, 100000, 'even']]` (dash form, 2.15) |
| `Прва 2-100 на парној страни` | `[[2, 100, 'even']]` |
| `Белодримска непарна страна` | `[[1, 100000, 'odd']]` (whole odd side) |
| `Лазара Мићуновића 19-до краја и 44-до краја` | `[[19, 100000, 'odd'], [44, 100000, 'even']]` |

> **Downstream:** an `OPEN_END` interval is matched by the normal interval logic
> (`_bounds_ok`/`houseInInterval`) — `100000` is simply a very large `hi`, no special case in
> stage04. The Worker renders it as an empty upper‑bound field with a "до краја" placeholder
> (see [08](08-worker-live-preview.md)).

---

## 2.13 The `бр.` / `број` house‑number label (`is_broj_token`)

**Rule:** `бр`/`бр.`/`број`/`броја`/`бројеви` (and Latin `broj…`) is a *label* introducing
house numbers — neither part of the street name nor a house number itself.

- In `parse_compact`, it **ends the street name** (the name/number boundary breaks on it).
- In `_add_numbers`, it is **dropped** from the number side.

### Examples

- `Нова 27 бр. 5-9` (no register match for `Нова 27`) → street `Нова`, single `27`,
  interval `[5, 9, 'odd']`.
- `Нова 27 бр. 5-9` (when `НОВА 27` **is** a register street, see 2.14) → street `Нова 27`,
  interval `[5, 9, 'odd']`.

---

## 2.14 Numbered street names — `Нова 27`, `Улица N` (register‑driven)

**Rule:** a trailing number that, together with the name so far, is **itself a register
street** is kept as part of the **name**, not stripped as a house number. Register‑driven via
the same `is_street` predicate as the compound‑`и` merge (2.8).

Guards:
- Requires a **name stem before the number** (`j > 0`) — so a bare number continuing the
  previous street (`Стројковце 0 и 1`) is **never** promoted to a street, even if `1` happens
  to be a register street name elsewhere in the muni.
- The whole prefix `"<name so far> <number>"` must normalize to a known register street.

### Examples

| Input | register has… | Result |
|-------|---------------|--------|
| `Нова 4, Нова 6, Нова 21` | `НОВА 4`, `НОВА 6`, `НОВА 21` | three segments: `Нова 4`, `Нова 6`, `Нова 21` |
| `Нова 4, Нова 6, Нова 21` | *(no predicate)* | one segment `Нова` (numbers 4/6/21 collapse) |
| `Улица 27` | `УЛИЦА 27` | one segment, street `Улица 27` |
| `Стројковце 0 и 1` | `1` is a street | one segment `Стројковце` (number not promoted) |

### Rationale

Niš and others have streets literally named `Улица N` / `Нова N`. Without this rule each
`Нова N` parsed as house N of a single `Нова` street, collapsing `Нова 4, Нова 6, Нова 21, …`
into one `Нова` street + houses 4/6/21. Fixed at parse time (register‑driven) for the same
positional‑id reason as the `и` merge.

> **Pipeline note:** stage03's `is_street` set now also includes register names containing a
> **digit** (not only those with the token `И`) so this predicate is populated — see the
> note under 2.8.

---

## 2.15 Text preprocessing in `parse_coverage`

Before dialect detection, `parse_coverage` runs these regex fix‑ups on the raw text so the
tokenizer sees clean tokens (the preamble strip runs **first**, so its `улицама` can't skew
dialect detection):

| Regex | Fix | Example |
|-------|-----|---------|
| `_LIST_PREAMBLE_RE` | drop a prose list‑introducer ending `у улици:` / `у улицама:` | `…у МЗ Беочин град у улицама: 1.маја, …` → `1.маја, …` |
| `_DEO_GLUE` | split `део` glued to a number | `део13` → `део 13` (see 2.7) |
| `_NUM_DO_DASH` | split a dash standing in for `од … до` | `98-до краја` → `98 до краја` (see 2.12) |
| `_DASH_SPACE` | collapse spaces around a range dash, **digits only** | `2- 100`, `2 - 100` → `2-100` |
| `_ORDINAL_GLUE` | split an ordinal glued to the next word | `7.јула` → `7. јула` |
| `_HOUSE_NUM_DOT` | drop a house number's trailing dot in a number context | `52. и 54.` → `52 и 54` |

- **`_LIST_PREAMBLE_RE`** strips up to and including a `…у улиц(и|ама):` marker. Some docs
  (Беочин) prefix the list with a sentence ("voters residing in MZ … in the street(s):") that
  the compact parser would otherwise **glue onto the first street**. Worse, the preamble's
  `улицама` matches the structured `Улица:` label, so a station whose list also contains a
  `број` token (e.g. `Светосавска од броја 6-14`) was mis‑detected as **structured** and its
  **entire** coverage collapsed into one whole‑street segment — stripping the preamble fixes
  both. The colon‑terminated marker occurs **only** in this preamble nationwide (16 Беočин
  stations); the structured `Улица:` label is never preceded by `у `, so structured docs are
  untouched.
- **`_DASH_SPACE`** only fires **between digits**, so block tags (`С-1`) and suffix tails are
  untouched. `Прва 2 - 100` → interval `[2, 100, 'even']` (would otherwise tokenize as `2`,
  `-`, `100`).
- **`_ORDINAL_GLUE`** keeps a glued ordinal in the street name: `7.јула 1-10` → street
  `7. јула`, interval `[1, 10, 'all']` (otherwise `7` would look like a house number).
- **`_HOUSE_NUM_DOT`** strips the trailing dot from a house number written `52.`/`54.` so it is
  no longer mistaken for a list ordinal (`is_house_token` rejects `^\d+\.$`). Fires **only** in a
  number-side context — when the dot is followed by a list separator (`,`/`;`/`и`), another
  number, or end of text — so an ordinal name word after it is untouched: `Церских јунака 52. и
  54.` → houses `52`, `54`, but `8. Март`, `7. јула`, `Краља Петра 2. део` keep their number in
  the name. Without this, `52.` glued into the street name and `54.` became a phantom segment
  that the OSM fallback then geocoded to an unrelated place (see [05](05-street-resolution.md)).

