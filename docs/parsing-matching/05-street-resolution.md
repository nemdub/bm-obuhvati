# 5. Street resolution

Code: `pipeline/stage04_match_addresses.py` — `resolve_settlement`, `resolve_street` and the
index builder `build_indexes`.

Resolves a raw street name to a **register street id**, scoped to the station's settlement
first and the municipality second. Returns `(street_id, method, score, ambiguous_ids)`. The
`method` drives confidence and review flags (see [07](07-review-flags.md)).

> Test target: `resolve_settlement`, `resolve_street`, `_strip_ulica`, `_sortkey`,
> `_part_streets`, `_token_subset`, `_fuzzy`, `_fuzzy_muni_unique`, and the alias map.

## 5.1 Scope: settlement‑first

Matching is **settlement‑first**. The scope hierarchy:

1. The segment's own `settlement_raw` if labelled (structured dialect), else
2. the station's **home settlement**, parsed from its address
   (`resolve_settlement_from_address`), else
3. the **inferred town settlement** (see 5.1.1) when the address has no settlement prefix, else
4. the **municipality** (group rep) as fallback inside `resolve_street`.

Everything is keyed by `config.group_rep(muni)` so one city document resolves streets across
all its city‑municipalities.

**Address order — settlement‑first OR settlement‑last.** RIK docs write the station address
both ways: settlement‑first (`"КЕЛЕБИЈА, ПУТ …"`) and settlement‑last
(`"Јована Грчића Миленка 5, Черевић"` — Beočin). `resolve_settlement_from_address` tries the
**first** comma token, then the **last**; first wins (settlement‑first is the common form,
and a street token rarely resolves to a settlement). Without the last‑token try, a
settlement‑last station found no home settlement, fell back to the eponymous town (5.1.1),
and had all its streets scoped muni‑wide — producing spurious `muni_fallback` flags and a
**conflict storm** as its streets mis‑matched the town's same‑named streets.

### 5.1.1 Eponymous‑town home‑settlement inference (`build_indexes`)

**Rule:** a station whose address has **no settlement prefix** (e.g. `ВУКА КАРАЏИЋА БР. 3` —
a town address) has its home settlement **inferred** as the **eponymous town settlement**: the
settlement whose name matches the municipality's. `build_indexes` computes one per muni by
running `resolve_settlement(muni_name, …)` — so Ваљево muni → `ВАЉЕВО` town (exact), and
Нови Београд → `БЕОГРАД (НОВИ БЕОГРАД)` (via the word‑containment fallback, 5.1 step 3 of
`resolve_settlement`).

These stations are tracked in `station_settlement_inferred`; the `settlement_inferred` flag is
threaded into `resolve_street` and re‑enables the muni‑wide fuzzy last resort (5.6).

#### Why

Town stations list streets without a settlement prefix, so their home settlement can't come
from the address — matching fell back to municipality‑wide. A common street name then went
`ambiguous`: `Владике Николаја` is a real street in 11 Valjevo settlements, so without knowing
the station sits in **Ваљево** town the matcher couldn't pick (Valjevo #16 had 7 such segments
stuck). Verified safe: a no‑settlement station **is** a town station (rural stations name their
village as the address, which resolves normally), so address‑resolved (rural) stations are
untouched — the inference only adds a fallback when address resolution returns `None`.

> Nationwide this resolved ~1,180 previously‑unresolved town streets (+21k links); 665 segments
> left the review queue outright. Newly‑resolved streets that several town stations share
> surface as honest `conflict` review items (previously hidden because nothing resolved).

### 5.1.2 Coverage settlement markers (`seg_marker_sett`, stage04 pass 1)

**Rule:** in rural docs the compact list **names a settlement, then lists its streets**
(`Копљаре, Бранислава Нушића, Карађорђева, Косовска, …`). Like `Насеље:` in the structured
dialect, that bare settlement name **scopes the streets that follow it** to that settlement. A
pre‑pass walks each station's segments in id (document) order: a **whole‑street** segment whose
normalized name is **exactly** a settlement of the muni becomes the current marker, recorded for
every later segment (`seg_marker_sett`).

The marker overrides **only an inferred town home** (5.1.1) — never a real address‑resolved
settlement — and sets `settlement_inferred = False` so the muni‑wide fuzzy last resort (5.6) no
longer fires for those segments.

#### Why

The home settlement comes from the address, but a rural address is often a *declined* form the
fuzzy match misses: Копљаре station's address `КОПЉАРИ` scores WRatio 85.7 against settlement
`КОПЉАРЕ` — below `FUZZY_MIN` (90) — so its home settlement was **inferred as the eponymous town
`АРАНЂЕЛОВАЦ`**. Its streets (`Карађорђева`, `Косовска`, `Николе Тесле` — names that also exist
in the town) then resolved muni‑wide to the **town's** streets, and a whole manual re‑assign was
needed. The coverage's first entry `Копљаре` **is** the exact settlement name, so it pins the
scope: all 23 streets now resolve `exact` in `КОПЉАРЕ`. Nationwide: **24 stations across 15
municipalities** (the rural village‑then‑streets pattern), **+344 links, −31 conflicts, −43
review flags**. Gated to inferred‑town homes and exact settlement names, so address‑resolved
stations are untouched.

### `resolve_settlement(raw, muni, settlements_by_muni)`

1. **Exact** normalized name match within the muni's settlements.
2. Else `rapidfuzz.WRatio` best ≥ `FUZZY_MIN` (90).
3. Else **single‑edit (Damerau‑Levenshtein ≤ 1)**, when the match is **unique** and both names
   are ≥ `SETT_EDIT_MIN_LEN` (6) chars. Addresses use a **declined** settlement name (`КОПЉАРИ`
   for register `КОПЉАРЕ`, `ВЕНЧАНИ`/`ВЕНЧАНЕ`) or a **single mistyped letter** (`ШАИНИВАЦ`/
   `ШАИНОВАЦ`, `НАФРЉЕ`/`НАДРЉЕ`) that WRatio scores ~85 — below 90. One edit (Damerau, so a
   transposition counts as one) is a typo/inflection; **two** edits already separate genuinely
   different places — `ДОЊА` vs `ГОРЊА ГРАБОВИЦА` is 2, `ТОПОЛА ВАРОШ` vs `ВАРОШИЦА` is 3 — so the
   distance‑1 ceiling is the guard. The length floor blocks short‑name flips (`БОР`/`БАР`), and
   the uniqueness test blocks a target one edit from two settlements at once. Nationwide: **9
   stations** newly resolve their home settlement, **0 false positives** (verified against every
   sub‑90 near‑miss); the other ~970 inferred‑home stations are genuine town addresses (best
   match < 70) and are correctly left to the eponymous‑town inference (5.1.1).
4. Else **unique word‑containment**: the target's word set ⊆ a settlement's word set, and
   **exactly one** settlement qualifies → that one. (Station addresses say `ЗЕМУН, …` while
   the register settlement is `БЕОГРАД (ЗЕМУН)`; WRatio length‑penalizes below 90.)
5. Else `None`.

## 5.2 Alternate keys built per settlement (`build_indexes`)

For every register street, beyond its literal `name_norm`, these **settlement‑scoped**
alternate keys are registered (a literal name always wins a tie):

- All `genitive_variants(norm)` (declension; see [01](01-normalization.md) §1.5).
- `_sortkey(norm)` and `_sortkey(g)` for each variant — order‑insensitive token keys.
- `_strip_ulica(norm)` — name with a standalone `УЛИЦА` word removed.

These let the doc form find the register form even when case, word order, or a literal
`УЛИЦА` differ — **deterministically**, counted as exact (unflagged).

## 5.3 The resolution ladder (`resolve_street`)

Tried in order; first hit wins. `primary` = normalized name with any parenthetical stripped;
`alt` = the parenthetical's normalized content (see 5.4).

| # | Step | Method returned | Scope |
|---|------|-----------------|-------|
| 0 | **Alias** substitution (before lookup) | `alias` (flagged) | muni‑wide replace |
| 1 | `primary` in settlement scope | `exact`/`alias` | settlement |
| 2 | `alt` in settlement scope | `exact` | settlement |
| 3 | `genitive_variants(primary)` in settlement scope | `exact`/`alias` | settlement |
| 4 | `_sortkey(primary)` / sortkeys of variants in settlement scope | `exact`/`alias` | settlement |
| 5 | `... 1 ДЕО` → base name | `exact`/`muni_fallback` | settlement, then muni‑unique |
| 6 | strip‑`УЛИЦА` → base | `exact`/`muni_fallback` | settlement, then muni‑unique |
| 7 | `_part_streets` (base → numbered parts) | `base_parts` (flagged) | settlement, then muni |
| 7a | `_locality_streets` (single word → заселак/locality cluster) | `locality` (flagged) | **settlement only** |
| 8 | `_fuzzy(primary)` | `fuzzy` (flagged) | **settlement only** |
| 9 | `_token_subset(primary)` | `fuzzy` (flagged) | settlement |
| 9a | `_initial_abbrev_match` (initial / title abbreviation) | `abbrev` (flagged) | **settlement only** |
| 10 | muni exact (`primary`, then `alt`) | `muni_fallback` / `ambiguous` / `exact` | muni |
| 11 | `_fuzzy_muni_unique` | `fuzzy` (flagged) | **muni, only if no home settlement OR an inferred town** |
| 12 | settlement‑name (village) claim | `settlement` (flagged) | muni, **last resort** |
| — | nothing | `none` | — |

`method == "exact"` becomes `"alias"` when an alias rewrote the name (so an aliased exact
match is still surfaced for review).

`muni_fallback` is only returned when the station **has** a home settlement (`settlement_id`
truthy); a station with no settlement gets plain `exact` from muni scope.

The ladder is lexical only. Segments it returns `none`/`ambiguous` for get a second chance
in the **geographic proximity pass** that runs after pass 1 — see 5.14.

## 5.4 Parentheticals (`_PAREN_RE`)

**Rule:** parentheticals are alternate / provisional names (`Елека Бенедека (493. нова)`,
`Корзо (Бориса Кидрича)`), **not** part of the street name. They are **stripped** for the
primary match key and tried only as an **exact** alternate (`alt`) — **never fuzzed**.

### Rationale

Mashing the parenthetical into the key let a noisy fuzzy match e.g. `493 нова` → `3. нова`.
Stripping it kills that. (Old‑name restatements — the same street listed twice, once
parenthesized — are handled separately in stage04's main loop, see
[06](06-claim-resolution.md) §old‑name‑dup.)

## 5.5 Fuzzy matching (`_fuzzy`) — settlement scope only

**Rule:** `rapidfuzz.WRatio` best match ≥ `STREET_FUZZY_MIN` (90), **within the station's own
settlement only**. Plus a **digit guard**: if the numeric tokens of the doc name differ from
those of the matched register name, reject (different streets).

### Examples

- `Виноградска` → `ВИНОГРАДАРСКА` (typo, same settlement) → `fuzzy`.
- `1 ДЕО` vs `10 ДЕО` → digit guard **rejects** (different numbers).
- `7 ВОЈВОЂАНСКЕ` vs `8 ВОЈВОЂАНСКЕ` → digit guard **rejects**.

### Rationale (why muni‑wide fuzzy was removed)

Municipality‑wide fuzzy invented matches for streets that don't exist (`Ернеа Лањија`,
absent from the register, was wrongly fuzzy‑matched to a Palić street). Fuzzy is now allowed
**only** within the home settlement (catches local typos); across the municipality only
**exact** matches count. The digit guard removed ~3.6k *wrong* links (unresolved +489 honest,
conflicts −80).

## 5.6 Muni‑wide fuzzy exception (`_fuzzy_muni_unique`)

**Rule:** for stations with **no resolvable home settlement** *or an **inferred** town scope*
(`settlement_inferred`, see 5.1.1), a *much stricter* muni‑wide fuzzy runs:

- cutoff `STREET_FUZZY_MUNI_MIN = 93` (vs 90),
- same digit guard,
- fires **only** when exactly **one** register name clears the cutoff **and** that name maps
  to exactly **one** street (uniqueness guard).

Gated on `if not settlement_id or settlement_inferred`.

### Rationale

A no‑settlement station never runs the settlement‑scoped fuzzy (step 8), so a one‑letter doc
typo like `Михаила` → `Михајла` Пупина would otherwise fall through to `no_match`. An
**inferred‑town** station *does* have a (town) scope, but the town doc may reference a
peri‑urban street the register files under a neighbouring settlement — so it keeps the same
last resort (this also recovers the 32 cross‑settlement fuzzy matches the town inference would
otherwise have dropped). The uniqueness requirement keeps it from reintroducing invented
matches: a typo'd nonexistent street would, at most, near‑miss one real name and stays
unresolved. Flagged `fuzzy` for reviewer confirmation.

> An **address‑resolved** (rural) station keeps muni‑wide fuzzy **off** — `settlement_inferred`
> is false — preserving the original guard against muni‑wide invented matches (5.5).

## 5.7 Municipality exact fallback / ambiguity (step 10)

**Rule:** if `primary` (then `alt`) exists in the municipality scope:

- maps to **exactly one** street → `muni_fallback` (flagged; plausible cross‑settlement
  coverage), or plain `exact` if the station has no home settlement.
- maps to **several** streets (same name in multiple settlements) → `ambiguous`, returns
  the candidate ids, **links nothing** (picking one would be a coin flip).

### Rationale

`muni_fallback` halved by requiring exactly one candidate. `Николе Тесле` exists in 7 Sombor
settlements → `ambiguous` (the `ambiguous:SETT1|SETT2|…` reason lists them). This halved
conflicts 2,717 → 1,443.

## 5.8 `_strip_ulica` (standalone `УЛИЦА`)

**Rule:** drop a standalone `УЛИЦА` word from a multi‑word name. Applied **symmetrically** —
also registered as a register‑side alternate key. `Поручничка улица` ↔ register `ПОРУЧНИЧКА`;
register `ЗМАЈЕВА УЛИЦА` ↔ doc `Змајева`. (Single‑word `УЛИЦА` is not stripped.)

## 5.9 `_part_streets` and `... 1 ДЕО` (base → numbered parts)

**Rule (`_part_streets`):** a plain base name claims **all** register streets that are
numbered parts of it — `name.startswith(base + " ")` and the remaining tokens are all digits
or `ДЕО`. `Војни Пут` → `ВОЈНИ ПУТ 1` + `ВОЈНИ ПУТ 2`. The first id is the anchor; the rest
ride in the 4th return slot (`ambiguous_ids`) and are all claimed. Method `base_parts`
(flagged).

**Rule (`... 1 ДЕО`):** the register's *first* part of an `N ДЕО` street is the plain base
name (parts start at 2). `Угриновачки пут 1 део` → strip ` 1 ДЕО` → `УГРИНОВАЧКИ ПУТ`,
matched in settlement scope (or muni‑unique).

> Caveat: real RIK coverage gaps exist near these (Zemun Vojni‑put area — `Павла Вујисића`
> with 119 addresses is never mentioned; `Поручничка` only as a nonexistent `16а`). Those are
> genuine document gaps, not matcher bugs.

## 5.10 `_token_subset` (surname containment)

**Rule:** a unique settlement street whose name **contains all** of the doc name's words
(doc has ≥ 2 words) **and** shares the same **last** word (surname), and is strictly longer.
Ties are rejected. `ВУКА КАРАЏИЋА` ⊂ `ВУКА СТЕФАНОВИЋА КАРАЏИЋА`. WRatio under‑scores these
(length penalty ~85), so they need their own rule. Returned as `fuzzy` (flagged).

## 5.10a Initial / title abbreviation (`_initial_abbrev_match`, step 9a)

**Rule:** some docs abbreviate a **given name to its initial** (and a title to its short form),
spelling out only the surname: `М.Пупина` for `МИХАЈЛА ПУПИНА`, `Ф.Вишњића` for `ФИЛИПА
ВИШЊИЋА`, `Др В.Војиновића` for `ДР ВЛАДИМИРА ВОЈИНОВИЋА`, `Н. Х. Рада Кончара` for `НАРОДНОГ
ХЕРОЈА РАДА КОНЧАРА` (double initial), `Проф Војислава Бабића` for `ПРОФЕСОРА ВОЈИСЛАВА БАБИЋА`.
Normalization already splits the abbreviation dot into a standalone single‑letter token
(`Б.Марковића` → `Б МАРКОВИЋА`) and expands `Др` → `ДОКТОРА`, so the doc and register align
position‑by‑position.

Matched **positionally** against each settlement street's **canonical** `name_norm` (not the
declension/sortkey alt keys, whose reordering would let an initial land on the wrong word), with
the **same token count**:

- a **single‑letter** doc token matches a register word by **first letter** (`М` → `МИХАЈЛА`);
- a **title abbreviation** matches its spelled‑out form (`_TITLE_ABBREV = {ПРОФ: ПРОФЕСОРА}` —
  `ДР` is already handled in normalization on both sides; `ПРОФ` can't be, because the register
  itself stores some streets abbreviated `ПРОФ.`, so it's a token equivalence here instead);
- every **other** token must match **exactly** (the surname is the anchor).

Returned only when the match is **unique** — two given names sharing an initial *and* surname
(`МИХАЈЛА ПУПИНА` vs `МИЛАНА ПУПИНА` → both `М ПУПИНА`) are an unresolvable coin flip and skip.
Settlement scope only (an inference). Method `abbrev`, conf 0.6, **flagged**; the Worker appends
„doc name“ → „register name“ like fuzzy/alias so the reviewer confirms each expansion.

### Why / scope

A reviewer hit a Смедеревска Паланка station where ~18 streets were written this way and had to
re‑assign each by hand. The rule resolves them automatically (only a genuine colloquial name like
`Улица Дисова` → `ВЛАДИСЛАВА ПЕТКОВИЋА ДИСА`, not an abbreviation, still needs manual review).
Nationwide: **71 segments across 11 municipalities**, **+~2,200 links, −89 unresolved streets, 0
new conflicts** (the unique‑surname constraint makes false matches near‑impossible — a normal
fully‑spelled street has no single‑letter token, so the rule never fires on it).

## 5.11 Street aliases (`config.STREET_ALIASES` → `_ALIASES`)

**Rule:** a hand‑maintained `(municipality_id, normalized doc name) -> normalized register
name` map. The alias **replaces the doc name before lookup**, municipality‑wide. Alias hits
report method `alias` (NOT silent exact): conf 0.6, `needs_review=1`, reason `alias` — the
reviewer must confirm each hand‑asserted substitution.

Current entries: Sombor `Пинкијева` → `Хероја Пинкија`; Majdanpek `Нушићева` →
`Бранислава Нушића`.

### ⚠️ Caution

Aliases replace the name **municipality‑wide and before lookup**. In a muni where the doc
form is *also* a real register street, the alias hijacks correctly‑matching stations
(verified: a Požarevac `Нушићева` → `Бранислава Нушића` alias broke 4 stations whose city
`НУШИЋЕВА` was exact‑matching). Before adding an alias, confirm the doc form isn't a real
street anywhere in that muni — or scope the fix to a single station via the UI street picker.

## 5.12 Settlement‑name (village) claims (step 12, last resort)

**Rule:** some stations name a whole **settlement** instead of streets — either bare
(`Белосавци` in Topola) or with an explicit marker. `_strip_settlement_prefix` removes a
leading **`НАСЕЉЕ `** or **`НАСЕЉЕНО МЕСТО `** (the latter is how every Vladimirci station is
written: `насељено место Белотић`). If the de‑prefixed `primary` matches a settlement name in
the muni, claim **every street** of that settlement (first id anchor, rest in `ambiguous_ids`).
Method `settlement`, score 85, reason `settlement_claim:НАЗИВ` (flagged).

> The marker must be the **nominative** whole‑settlement form. `део насељеног места <X>`
> ("**part of** settlement X", genitive) is deliberately **not** stripped: it claims only a
> hamlet/`мала` of X (spelled out in the following clauses), so claiming the whole settlement
> would over‑cover — those segments stay unresolved for review.

This is the **last resort** — tried only when nothing matched anywhere in the municipality.
Earlier placement hijacked cross‑settlement street matches.

> The claim kind for these is `sett_whole` at the lowest specificity — see
> [06](06-claim-resolution.md) §specificity. It yields to **any** street‑level claim.

## 5.13 Reviewer overrides applied here

After machine resolution, stage04 applies reviewer overrides from `overrides.json`
(exported from D1):

- `manual_street_id == "none"` → `method = "manual_none"`, no street, treated **resolved**
  (the reviewer confirmed the street genuinely doesn't exist; no links/polygon built).
- `manual_street_id == "sett:<id>"` → whole‑settlement claim (`manual_settlement`), like a
  document village claim.
- `manual_street_id` present and in the register → `method = "manual"`, conf 0.9, unflagged.
- `manual_json` present → replaces the machine parse for claim building.

These mirror the Worker's override resolution (see [08](08-worker-live-preview.md)).

## 5.14 Proximity fallback & disambiguation (post‑ladder pass)

The ladder above is **purely lexical** — it deliberately refuses muni‑wide fuzzy for
ordinary stations (5.5) because matching by name alone invents matches for nonexistent
streets. **Geography is the missing constraint that makes a cross‑settlement reach safe
again:** a polling station covers a contiguous neighbourhood, so a street the ladder left
unresolved is almost always physically near the streets the station *already* matched — and
it should be one **no other station has claimed**.

So, *after* pass 1 (and reviewer/added claims) and *before* pass 2, a **proximity pass**
(`stage04 main()`) revisits every segment whose method is `none` or `ambiguous`:

- **Anchor** (`_station_anchor`): the centroid of the station's already‑resolved‑street
  centroids (`street_centroid`, built in `build_indexes` from address UTM `x`/`y`). A
  station with **no** resolved street has no anchor and is **skipped** — there's no sibling
  coverage to judge proximity against.
- **Adaptive radius**: `clamp(PROXIMITY_RADIUS_FACTOR × extent, FLOOR, CAP)` where `extent`
  is the max distance from the centroid to any of the station's resolved streets — tight in
  dense cities, wider in sparse villages (`config.PROXIMITY_RADIUS_*`).
- **Candidate pool**: register streets in the station's `group_rep` muni (same scope as the
  rest of stage04) that are **unclaimed** (not in `claims_by_street`) and have a centroid,
  indexed per‑muni with a `scipy.spatial.cKDTree` and queried with `query_ball_point`.
- **Pick** (`_nearest_unclaimed`):
  - `ambiguous` → restrict candidates to the segment's own same‑named `amb_ids`; take the
    **nearest unclaimed** one (pure tie‑break among genuinely same‑named real streets).
  - `none` → keep candidates whose name clears `STREET_FUZZY_PROX_MIN` (reusing `_fuzzy`'s
    **digit guard**); take the nearest. Two different streets exactly equidistant → skip.
- On a hit: `method = "proximity"`, the street's claims are emitted (`_emit_claims`) so
  pass 2 links them like any other claim, and a local `newly_claimed` set stops two
  unresolved segments grabbing the same street.

Confidence **0.5**, reason `proximity` (flagged) — every proximity match is surfaced for
review, with the Worker appending the „doc name“ → „register name“ discrepancy (7.5).

**Incremental `--municipalities`** stays correct: it loads *all* segments of the affected
`group_rep` munis and proximity is muni‑scoped, so the `claimed` snapshot and the
per‑station anchors are complete within scope.

## 5.15 Sub-locality / hamlet (заселак) claims (`_locality_streets`)

**Rule:** some inhabited localities have **no own naselje** in the register — they're encoded
as a **name prefix on several streets of the parent settlement**. Sombor #10 covers
„Ранчево", which the register stores as 5 streets in the СОМБОР naselje: `РАНЧЕВО ХИЛАНДАРСКА`,
`РАНЧЕВО ВУКА КАРАЏИЋА`, `РАНЧЕВО ЖАРКА ЗРЕЊАНИНА`, `РАНЧЕВО-МИЛУНКЕ САВИЋ`,
`ЗАСЕЛАК РАНЧЕВО РЕЛИЋИ`. A **single-word** coverage that is the locality token of **≥2
distinct** such streets claims them all (anchor + rest in `ambiguous_ids`). Method `locality`,
score 80, conf 0.7, reason `locality` (flagged). Placed before `_fuzzy` so the cluster isn't
hijacked into matching just one of its streets (the pre-existing bug: „Ранчево" linked 8 of 63
addresses).

The locality token is the street's **first** word, or the word right after a leading
`ЗАСЕЛАК`. Guards keep it from inventing clusters:
- **single-word, non-numeric** `primary` (a multi-word „ЦАРА ДУШАНА" can't sweep „ЦАРА ЛАЗАРА");
- **canonical names only** — `by_sett_norm` also holds declension/sortkey *alt* keys that
  point back at the same or an unrelated street (`НИКОЛЕ ЛУЊЕВИЦЕ` surfaces under the alt key
  `ЛУЊЕВИЦА …`; `ДОЊА БРДА МАЛА` under the sortkey `БРДА …`); only a key equal to its street's
  `name_norm` counts;
- **≥2 distinct street ids** (so one street under two declension keys isn't a "cluster");
- a **stoplist** of generic structural words (`_LOCALITY_STOP`: ЗАСЕЛАК, НАСЕЉЕ, САЛАШ, ПОТЕС,
  МАХАЛА, ПУТ, БЛОК, ТРГ, КРАЈ, …) that lead many unrelated streets.

Nationwide this resolves 4 genuine localities (Ранчево, Шапоње, Билић, Багљаш); each is
flagged for review.

> Note: this is **not** a settlement — Ранчево is absent from both the register settlements
> and `data/naselje.csv`. A settlement-polygon source cannot cover it; the locality claim is
> the right mechanism. See 5.12 for *real* whole-settlement (village) claims.

## 5.14 (cont.) Proximity worked example

> Worked example — `Рзавска` (Arilje area): the doc street isn't in the station's home
> settlement, and `РЗАВСКА` exists in several settlements, so the ladder returns
> `ambiguous` (nothing linked). The proximity pass picks the `РЗАВСКА` in `АРИЉЕ`, the
> settlement nearest the station's other matched streets, and links it.

## 5.16 OSM (Nominatim) fallback — last resort, draws geometry the register lacks

**When it fires.** After the proximity pass, for the segments still `none` / `ambiguous` —
plus the **weak‑substring `fuzzy`** matches described below. These name a street or settlement
the register **cannot place at all** — not a spelling the ladder missed, but a locality with no
own naselje and no clean street encoding. Both kinds fall back: streets and settlements/places.

**Weak‑substring fuzzy (`_weak_substring_fuzzy`).** A single‑word coverage can be caught by
WRatio's partial ratio as a **non‑leading substring** of a longer street — `Жарковац`
fuzzy‑matches `БРАНКА РАДИЧЕВИЋА ЖАРКОВАЦ`, linking *one* street of a whole hamlet. When the
doc token is a single word that is a non‑leading word of the matched street's name, the segment
is routed to the OSM fallback (place query only). On a **hit** the wrong fuzzy claim is pulled
and the OSM place polygon becomes the coverage; on a **miss** the fuzzy match is kept untouched.
This is what makes the worked example below resolve.

**Accepting only real places/streets.** A bounded muni search returns *some* object for any
generic token (`ЗГРАДА`→building, `СТАДИОН`→stadium, `ЦИГЛАНА`→brickworks, `1 УПРАВА`→a single
house), so OSM hits are filtered by `class`/`type`: settlements accept `place` (hamlet, village,
suburb, locality, …) or `boundary`=administrative; streets accept `highway` (minus footway/path/
…). Everything else is rejected. The geocoder asks for the top 5 candidates and keeps the first
that passes, so a spurious top hit doesn't mask a real match just below it.

**Worked example — Sombor #20, „Жарковац, Задружна".** `Задружна` resolves to a Sombor
street; `Жарковац` does not. `Жарковац` is a **hamlet of Sombor** the register stores only as
*suffixes* on a few streets (`ПАРТИЗАНСКА-ЖАРКОВАЦ`, `БРАНКА РАДИЧЕВИЋА ЖАРКОВАЦ`, …), several
already **retired** with no live addresses — so 5.15's locality rule (which keys on a *prefix*)
doesn't fire and there are no points to tessellate. The only register **settlement** polygon
literally named `ЖАРКОВАЦ` sits in **Ruma, ~150 km away** — drawing it would be flat wrong.

**Municipality scoping is the crux.** We geocode the name against OpenStreetMap
(`/search`, `polygon_geojson=1`, `countrycodes=rs`) **bounded to the station's municipality
bbox** (`viewbox` + `bounded=1`, from `load_muni_boundaries()`) plus a structured
`city`/`county` = muni name. That returns *Sombor's* Жарковац, not Ruma's. The result geometry
is then **clipped to the municipality boundary** as a second guard.

**Two more guards against drawing the wrong place** (a municipality can be large, so the bbox
scope alone still lets a common street name land on a same-named place in another town):

- **No letterless query.** A name with no alphabetic characters (a bare `54` left by a mis-parsed
  house number, see [02](02-coverage-parsing.md) §2.15) is never geocoded — Nominatim would
  resolve it to an unrelated numbered admin relation.
- **Distance to the station's own coverage.** When the station already has resolved-street
  coverage, an OSM claim farther than `OSM_MAX_COVERAGE_DIST_M` (≈3 km, mirroring
  `PROXIMITY_RADIUS_CAP_M`) from **every** resolved-street centroid is rejected — it left the
  station's real neighbourhood (a polygon over another town's centre). Stations with no resolved
  coverage have no anchor and are exempt (OSM is then the only signal). Rejected segments stay
  unresolved and flagged — an honest coverage gap, never a wrong far-away polygon. stage04 prints
  the rejection count (`OSM claims rejected as far from coverage`).
- **Empty coverage gets no shape** (stage04, `_has_coverage`). The OSM claim draws the *whole*
  geocoded street/area, so it is only meaningful when the segment actually claims coverage. A
  segment whose effective parse is empty (no whole / intervals / singles / бб) is skipped. This
  also gives reviewers an intuitive off-switch: **clearing a segment's coverage drops its OSM
  polygon** (the "doesn't exist" button — method `manual_none` — already suppresses it too).

**Overlap with other stations' coverage → rejected in stage05** (`_osm_foreign_overlap`,
`OSM_FOREIGN_REJECT_MIN`). A whole-street OSM line for a street the register can't place (e.g. a
town `Петра Драпшина` absent from the register, drawn in full) runs **alongside a neighbouring
registered street** and its buffer covers addresses that belong to **other** stations — a visible
violation of one-address-one-station. stage05 counts matched addresses inside each OSM claim,
split own vs other; the claim is **dropped** when it contains ≥ `OSM_FOREIGN_REJECT_MIN` (10)
foreign matched addresses AND more foreign than own (a legitimate register-gap claim sits on
addresses the register *lacks*, so few/no foreign points fall inside it). A claim where the
station has the larger share — e.g. a real settlement polygon covering its own village plus a few
neighbours — is kept. Prints `OSM claims rejected (overlap other stations' coverage)`.

**Geometry is the coverage.** There is no register street id and no address links — stage05
draws the OSM geometry directly (unioned with any point-Voronoi cells the station also has),
exactly like a whole-settlement claim. An OSM **area** is used as-is; a **street LineString**
is buffered by `OSM_STREET_BUFFER_M`, a bare **place node** by `OSM_POINT_BUFFER_M`. Method
`osm`, confidence 0.5, reason `osm_fallback` — **always flagged for review**, because a
buffered point/line is an approximation a human should confirm or redraw. The review note tells
the reviewer how to remove it (clear coverage / "doesn't exist") and that the map updates on the
next recompute (OSM polygons live in the stored R2 layer, not the live point preview).

**Caching (committed) & offline mode.** Every Nominatim response — hit *or* miss — is cached
in `data/osm_cache.json`, keyed `kind|muni_id|normalized_name`, and **committed to the repo**,
so a recompute never re-queries a name and coverage stays reproducible across clean checkouts /
CI. Public Nominatim is rate-limited to ≤1 req/s with a descriptive User-Agent (`NOMINATIM_URL`
points at a self-hosted instance to lift the limit). `OSM_OFFLINE=1` runs **cache-only** — a
miss returns nothing without touching the network — and the matcher's unit tests run that way,
so they never hit OSM. To re-try a name OSM lacked, delete its key from the cache file.

**Incremental `--municipalities`** merges `osm_claims.parquet` the same way the
settlement-claim map is merged: drop the affected stations' rows, append the fresh ones.
