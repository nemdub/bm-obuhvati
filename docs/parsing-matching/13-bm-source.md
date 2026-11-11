# 13 — BM station source (bm.itblokada.org)

Since 2026-11 the station list and coverage text come from the **bm.itblokada.org** export
instead of the 2022 RIK Word documents. This document specifies what changes; everything not
mentioned here (normalization, the street-resolution ladder, claim specificity, geometry)
works as described in documents 01–12.

`config.STATION_SOURCE = "bm"` selects it. `"rik"` restores the old path
(`stage02_extract_docs.py`, city groups, amendments) — see [Rollback](#rollback).

## 13.1 Fetching — `stage02_fetch_bm.py`

```
GET https://bm.itblokada.org/api/stations/export?format=json&include=covered_addresses
X-Api-Key: $BM_API_KEY          # env var, or BM_API_KEY=… in the gitignored repo-root .env
```

One response holds every station of the current register. The raw response is saved to
`data/bm/export-<date>.json` (+ `latest.json`) so a build is reproducible;
`--from-file` rebuilds from a saved export without calling the API.

| BM field | `stations.parquet` | note |
|---|---|---|
| `municipality.id` | `municipality_id` | register *matični broj* — same key as ours |
| `number` | `number` | printed station number, unique per locality |
| `venue_cyr` / `venue_lat` | `name_cyr` / `name_lat` | venue without the address tail |
| `address_cyr` / `address_lat` | `address_cyr` / `address_lat` | `"<Settlement>, <street> <no>"`; empty for ~2% |
| `covered_addresses` | `raw_coverage_text` | verbatim decision text |
| `id` (ULID), `register_id`, `locality_id` | `bm_id`, `register_id`, `locality_id` | cross-reference keys |
| `voter_count`, `geo.lat/lon` | `voter_count`, `lat`, `lon` | |
| `settlement.id/name_cyr` | `bm_settlement_id`, `bm_settlement_cyr` | settlement the venue sits in |

**Station id** = `int(municipality_id) * 100000 + number` (`station_id()`), e.g. Ada no. 1 →
`8001200001`. The RIK stage used a positional counter that shifted every later id when a
station was inserted; this one depends only on the printed number. The stage aborts if the id
is not unique.

**Skipped** (listed with the reason in `artifacts/bm_skipped_stations.csv`):

- `tag` = `diaspora` / `prison` — special groups, no coverage text;
- municipality not in the address register — the Kosovo and Metohija municipalities (`90xxx`):
  there are no register addresses to build a polygon from.

`amendments_raw.parquet` is written **empty**: the BM text is the current decision, so
stage03b is a pass-through (it still refreshes the `*_pristine` snapshots stage03c needs).

`config.CITY_GROUPS` is empty under this source: Vranjska Banja, Kostolac and Sevojno have
their own stations in the current register, so nothing is scope-merged or hidden.

## 13.2 The BM dialect — `coverage_parse.parse_bm`

The text is machine-generated, one clause per street, every house number listed:

```
Улица 8.МАРТА бројеви 1, 1-А, 1-Б, 2, 3, ; Улица БЕЧЕЈСКИ ПУТ бројеви 1, 3, 4, 5, ;
```

There are no ranges, no parity words and no settlement labels. `parse_coverage(text, bm=True)`
routes a text whose first clause matches `Улица … бројеви …` to `parse_bm` (stage03 passes
`bm=config.STATION_SOURCE == "bm"`); any other text — e.g. a reviewer-corrected free text —
still goes through the RIK dialects of document 02. Segments get `parse_dialect = "bm"`.

| input | output | why |
|---|---|---|
| `бројеви 1, 1-А, 5-И` | singles `[1,""] [1,"А"] [5,"И"]` | letter suffix after `-` |
| `бројеви 23-2, 6/2, 148-1` | singles `[23,""] [6,""] [148,""]` | **one house, not a range**: the sub-number is an entrance/flat; the register carries letter suffixes only, so it is dropped (a bare number already implies its suffixed variants, doc 06) |
| `бројеви 7-А/3` | single `[7,"А"]` | letter suffix kept, entrance dropped |
| `бројеви 5, 5/1, 5/2` | single `[5,""]` | duplicates collapse |
| `бројеви 0, 1, 2` | singles `[1,""] [2,""]` | `0` = no house number (as in doc 02) |
| `Улица ЗАСЕЛАК ЛИВАДА бројеви 0` | `whole_street` | a lone `0` claims the street at the lowest street-level specificity |
| `Улица 8.МАРТА …` | street `8. МАРТА` | ordinal un-glued for normalization |
| `Улица НЕМА УЛИЦЕ бројеви 0` | kind **`no_street`**, `whole` | not a street — see 13.4 |
| a clause that is not `Улица … бројеви …` | kind `unknown` | kept visible for review, never dropped |

## 13.3 Street resolution without settlement names — stage04 pass 1.6

The ladder of document 05 runs unchanged, scoped to the station's **home settlement**: the
settlement named in the station's address, else the BM settlement (`bm_settlement_id`, when it
belongs to the station's municipality), else the municipality's eponymous town.

Because the text names no settlement, a same-named street in another village is the main
risk. The explicit number lists resolve it — a list of house numbers is a fingerprint of the
street. `number_evidence_pick(listed, current, method, candidates, street_nums)`:

- candidates: every register street of the municipality with the segment's exact normalized
  name (plus the current match);
- it acts only when the lexical result is **unsure** (`none`, `ambiguous`, `muni_fallback`) or
  **visibly wrong** (fewer than 20% of the listed numbers exist on the matched street);
- a pick needs ≥ 2 listed numbers present (1 if the list has a single number), ≥ 50% of the
  list, and at least twice the hits of every other candidate — two villages with the same
  street name *and* similar numbering stay ambiguous for a reviewer.

**Home settlement first.** A street spelled exactly as in the text may exist in *another*
village while the station's own village has it under a slightly different spelling — and low
house numbers exist on almost every street, so the exact-name street elsewhere looks confirmed.
Real case, Pančevo no. 20 (Качарево): text «4. ОКТОБАР», Качарево register «4. ОКТОБРА»,
Банатски Брестовац register «4. ОКТОБАР» → it matched Брестовац and collided with station no. 2
there. So the candidates also include **near-spellings in the station's home settlement**
(`near_name_streets`: Damerau-Levenshtein similarity ≥ 0.8, identical number tokens), and:

- a home-settlement candidate that clears the thresholds and has at least as many hits as any
  candidate elsewhere wins **without** the 2× margin;
- a candidate elsewhere must beat the best home candidate by 2×;
- "home" is the home settlement only — not the derived multi-settlement set, which already
  contains the villages reached through the `muni_fallback` matches being second-guessed.

A pick sets method `numbers`, confidence 0.85, **not flagged** — when the picked name equals the
text or differs only by inflection (`same_word_forms`: same word count, each differing word pair
shares a stem — «ОКТОБАР»/«ОКТОБРА», «ЈУЛИ»/«ЈУЛА», «КОПАОНИЧКИ ОДРЕД»/«КОПАОНИЧКОГ ОДРЕДА»).
A near-spelling that is a different word («МИЛОША»/«МИЛАНА ОБРЕНОВИЋА», «РАДНИЧКА»/«РУДНИЧКА»)
gets method `numbers_near`, confidence 0.6, and **is flagged `fuzzy`** for a reviewer. It both re-resolves wrong
matches and confirms `muni_fallback` matches that previously always went to review.

## 13.4 «НЕМА УЛИЦЕ» — the no-street pass

About 40% of stations carry `Улица НЕМА УЛИЦЕ бројеви 0`: voters whose registered address has
no street. In practice these are houses of the station's home settlement on streets the voter
roll does not know — villages whose streets were named later, or never.

After every street-level claim is known (ladder, number evidence, proximity, OSM), a
`no_street` segment claims **the register streets of the home settlement that no station
claims at all**. Streets that some station lists keep their unlisted houses for the Voronoi
step, exactly as before. `assign_no_street(ns_by_sett, free_by_sett, station_xy)`:

- one no-street station in the settlement → it takes every free house;
- several → each free house goes to the **nearest** of them (BM venue coordinates, UTM);
- several, none located → nothing is assigned (the Voronoi step decides).

Links get `match_method = "no_street"`, confidence 0.6. The segment has no `street_id` by
design and carries `review_reason = "no_street:<settlement>"` (informational, never flags).
If the station has no home settlement the reason is `no_home_settlement` and the segment **is**
flagged — the clause then covers nothing.

When a settlement is held *only* by its no-street stations (no other station has a house
there) each of them also gets a whole-settlement claim, so stage05 draws the official naselje
boundary — partitioned between co-claimants — instead of the ragged point cloud (doc 12).

What this cannot recover: the BM text does not say *which* villages a station serves. A
village with no station of its own and no listed street stays unclaimed and is filled by the
grow-to-border step of document 12.

## 13.5 Review flags

Document 07 applies, with these additions:

| code | flags? | meaning |
|---|---|---|
| `no_street:<settlement>` | no | no-street clause resolved to the home settlement |
| `no_home_settlement` | yes | no-street clause on a station without a home settlement |
| `no_match` on a BM segment listing ≤ `config.BM_NO_MATCH_INFO_MAX` (2) houses | no | the list is voters' registered addresses; a stray house the register does not carry is routine and not fixable by a reviewer. Longer lists with no hit still flag (wrong street) |

`parity_unconfirmed`, `amendment` and range-related flags do not occur: the dialect has no
ranges and the source has no amendments.

## 13.6 Worker

- Migration `0014`: `polling_stations` gains `bm_id`, `register_id`, `voter_count`, `lat`, `lon`.
- `/api/s/:id/segments` reports a `no_street` segment as resolved (`street_resolved: true`,
  `no_street: true`) titled «Без улице — <насеље>», so it is not shown as an unresolved street.
- **Known gap:** the live preview (doc 08) does not reproduce the no-street assignment — it
  needs every station's claims in the settlement. The address dots of a station therefore omit
  its no-street houses until the next pipeline run; polygons (served from R2) include them.

## 13.7 Running

```
cd pipeline
../.venv/bin/python stage01_load_register.py
BM_API_KEY=… ../.venv/bin/python stage02_fetch_bm.py
../.venv/bin/python stage03_parse_coverage.py
../.venv/bin/python stage03b_apply_amendments.py
../.venv/bin/python stage04_match_addresses.py      # OSM_OFFLINE=1 to skip Nominatim
../.venv/bin/python stage05_voronoi.py
../.venv/bin/python stage06_build_sqlite.py
../.venv/bin/python compare_sources.py              # old vs new -> artifacts/bm-compare/REPORT.md
```

`compare_sources.py` pairs stations by municipality + number, **confirmed by address**
(numbering drifted since 2022), and reports totals, review reasons, per-municipality figures
and the polygon IoU of confirmed pairs.

## Rollback

`pipeline/backup_state.sh <label>` snapshots production D1 (full dump), the production R2
polygon blobs and the local artifacts into `backups/<date>-<label>/` with a `RESTORE.md`.
The pre-switch state is `backups/2026-11-11-pre-bm/` and git tag `pre-bm-source`. Reviewer
edits made against the RIK source (`segment_overrides` etc.) are keyed to the old positional
ids and the old text; they are preserved in that backup and are **not** applied to BM data.
