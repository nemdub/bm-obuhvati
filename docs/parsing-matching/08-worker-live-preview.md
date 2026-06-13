# 8. Worker live preview (TypeScript mirror)

Code: `worker/src/db.ts`

The review UI lets a reviewer edit a segment (numbers, parity, street pick, "doesn't
exist", "whole street", "бб") and see the coverage **live**, before the Python pipeline
re‑runs. So `db.ts` re‑implements the *matching* half of stage04/06 in TypeScript. **These
rules must stay in lock‑step with the Python side** — the canonical polygons come from the
pipeline; the Worker only previews.

> Test target (if the Worker gets unit tests): `houseInInterval`, `intervalParity`,
> `suffixRank`/`rankCmp`, `effectiveParsed`, `pointsForStation`, `resolveSegStreets`,
> `searchStreets`. Cross‑check each against its Python twin.

## 8.1 Shared constants

| Constant | Value | Python twin |
|----------|-------|-------------|
| `NONE_PICK` | `"none"` | `manual_street_id == "none"` (stage04 `manual_none`) |
| `ADDED_SEG_BASE` | `9_000_000_000_000` | `config.ADDED_SEG_BASE` |
| `SETT_PICK_PREFIX` | `"sett:"` | `"sett:"` prefix in stage04 overrides |
| `SUFFIX_AZBUKA` | `"АБВГДЂЕЖЗИЈКЛЉМНЊОПРСТЋУФХЦЧЏШ"` | `normalize.SUFFIX_AZBUKA` |
| `OPEN_END` | `100000` (in `app.js`) | `coverage_parse.OPEN_END` — open‑ended `до краја` upper bound |

### `OPEN_END` / `до краја` in the editor

The parser stores an open‑ended `до краја` range as `[lo, OPEN_END, parity]`
(`OPEN_END = 100000`, see [02 §2.12](02-coverage-parsing.md)). The interval matcher needs no
special case — `100000` is just a large `hi`. The editor (`app.js`) renders an `hi >= OPEN_END`
bound as an **empty** upper‑bound field with a "до краја" placeholder (i18n `toEnd`), and on
save an empty upper bound with a present lower bound is re‑encoded back to `OPEN_END`. So the
sentinel never shows as a magic number in the UI and round‑trips losslessly.

## 8.2 Interval matching (`houseInInterval`) — mirror of `_bounds_ok`/`_parity_ok`

```ts
houseInInterval(num, suffix, iv):
  [lo, hi] = iv
  if num < lo || num > hi: return false
  p = intervalParity(iv)                       // iv[2] if present, else odd/even/all from bounds
  if not (p=="all" || (p=="odd"&&num%2==1) || (p=="even"&&num%2==0)): return false
  loSfx = iv[3] || "";  hiSfx = iv[4] || ""
  if num==lo && loSfx && rankCmp(suffixRank(suffix), suffixRank(loSfx)) < 0: return false
  if num==hi && hiSfx && rankCmp(suffixRank(suffix), suffixRank(hiSfx)) > 0: return false
  return true
```

`suffixRank` maps each char to its azbuka index (unknown chars → `100 + charCode`); `rankCmp`
compares the rank arrays element‑wise (missing element = −1). This is the exact analogue of
`suffix_rank` + tuple comparison on the Python side. **Same suffix order, same `Д < Ц`
behavior.**

## 8.3 `pointsForStation` — mirror of the per‑house loop

For each candidate address of a station's resolved streets:

- **`house_num IS NULL`** → included iff `parsed.whole || parsed.bez_broja`.
- **interval** → `parsed.intervals.some(iv => houseInInterval(num, suffix, iv))`.
- **single** → exact `"{num}|{suffix}"` in the singles set, **or** bare `"{num}|"` in the set
  (bare implies suffixed variants).
- include iff `parsed.whole || inRange || isSingle`.

> Difference from the pipeline: the Worker has **no cross‑station conflict resolution** in
> preview — it just shows which addresses *this* station's claims would match. Specificity/
> bare‑number override across stations is only fully resolved by stage04. The memory notes
> the preview "approximates by matching the bare number to any suffix (no cross‑station
> override)". Tests should assert the *single‑station* matching matches Python, not conflict
> outcomes.

## 8.4 Settlement expansion (`resolveSegStreets`)

- `ov_street_id === NONE_PICK` ("none") → skip (no addresses).
- `ov_street_id` starts with `"sett:"` → expand to all `streets WHERE settlement_id = ?`.
- `review_reason` contains `settlement_claim` and no override → expand from the anchor
  street's settlement.
- otherwise → the single resolved street.

Mirrors stage04's `sett_whole` expansion and the `manual_settlement` / `settlement` claim
kinds.

> **D1 bind‑parameter cap.** A whole‑settlement claim can expand to **hundreds** of streets
> (a leading town heading like `ПОЖАРЕВАЦ` → all 534 town streets; `КРАГУЈЕВАЦ` → 1498). D1
> allows at most **100 bound parameters per query**, so the `street_id IN (…)` lookups in
> `pointsForStation` and `streetLinesForStation` are run in chunks of `D1_IN_CHUNK = 90` via
> `selectByStreetIds` and concatenated — binding all ids in one statement throws and returns a
> 500 for the whole station's map. (The spurious town‑heading claims themselves are a separate
> stage04 concern.)

## 8.5 Override resolution (`effectiveParsed`, `effStreet`)

- **`effectiveParsed(seg)`**: parse `ov_json ?? parsed_json` into
  `{intervals, singles, whole, bez_broja}` (defaults on parse failure). The manual JSON
  override wins over the pipeline parse — same precedence as stage04's `manual_json`.
- **`effStreet(s)`** = `ov_street_id ?? street_id` — manual street pick wins over the
  pipeline street. Same as stage04's `manual_street_id`.

The PUT/POST endpoints round‑trip `whole` and `bez_broja` flags so the checkbox state
survives.

## 8.6 Street picker (`searchStreets`)

- Scoped to the station's **municipality** (`SELECT municipality_id FROM polling_stations`).
- Needle = `%${q.toUpperCase()}%`.
- **Settlements first** (LIMIT 10), tagged `sett:<id>`, area=1 — lets a reviewer pick a whole
  settlement (village/area) claim.
- **Streets** (LIMIT 30), matched on normalized Cyrillic `name_norm` OR uppercase
  `name_lat`, area=0.

The picker auto‑opens for unresolved segments (`app.js`); min 2 chars (`/api/s/:id/streets?q=`).
Picking a street saves `street_id` to the override; "Улица не постоји" PUTs
`street_id:"none"`; revert deletes the override row.

## 8.7 Where the Worker intentionally diverges

| Aspect | Pipeline (canonical) | Worker (preview) |
|--------|----------------------|------------------|
| Cross‑station conflicts | resolved by specificity, flagged | not modeled |
| Proximity match ([05](05-street-resolution.md) §5.14) | geographic pass over all streets | **not mirrored** — needs the full register spatial index; the Worker just renders the stored result + localizes the `proximity` flag |
| Bare‑implies‑suffix override | one station wins per house | matches bare to any suffix locally |
| Polygons | Voronoi in stage05 | reads R2 blobs, no recompute |
| Street normalization | `normalize_street` (full) | relies on precomputed `name_norm` |

Tests for the Worker should target the **single‑station, deterministic** functions
(`houseInInterval`, `effectiveParsed`, `searchStreets` SQL shape) and pin them against the
Python equivalents so the two implementations can't silently drift.
