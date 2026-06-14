# Parsing & matching rules

This directory is the authoritative spec for how **bm-obuhvati** turns RIK polling‑station
Word documents into address links. It documents every parsing and matching rule the
pipeline (and the Worker's live preview) implements, including the dozens of edge cases
hardened over the project's history.

It is written to be the basis for an automated unit‑test suite: every rule is stated with
concrete **input → output** examples, the **rationale** (usually a real bug that motivated
it), and a **code reference** so the test can be pinned to the implementation.

## How the data flows

```
RIK .doc/.docx ──stage02──▶ stations.parquet (raw_coverage_text per station)
                              │
register CSVs ──stage01──▶ addresses / streets / settlements / municipalities
                              │
raw_coverage_text ──stage03──▶ segments_raw  (one segment per street clause; PARSING)
                              │
amendment docs ──stage03b──▶ segments_amended (surgical prose ops applied)
                              │
segments + register ──stage04──▶ segments (final) + links  (MATCHING)
                              │
links + addresses ──stage05──▶ polygons (Voronoi; geometry, not covered here)
                              │
everything ──stage06──▶ D1 SQLite + R2 blobs
```

The two rule‑heavy stages are **stage03 (parsing)** and **stage04 (matching)**; everything
they rely on lives in `pipeline/common/`. The Worker (`worker/src/db.ts`) re‑implements the
*matching* half in TypeScript so the review UI can preview a reviewer's edits live — its
rules must stay in lock‑step with stage04, which is why it gets its own document.

## Documents

| # | File | Covers | Primary code |
|---|------|--------|--------------|
| 1 | [01-normalization.md](01-normalization.md) | Building the Cyrillic match key for a street name; house‑number/suffix normalization; transliteration | `common/normalize.py`, `common/transliterate.py` |
| 2 | [02-coverage-parsing.md](02-coverage-parsing.md) | Turning a coverage cell into segments: compact vs structured dialect, number tokens, ranges, parity, `бб`, blocks, `део`, compound `и` names | `common/coverage_parse.py` |
| 3 | [03-document-extraction.md](03-document-extraction.md) | Reading stations out of `.doc`/`.docx`: table parsing, lone‑integer vs triplet fallback, sectioned city docs, table‑end trimming, municipality mapping | `stage02_extract_docs.py` |
| 4 | [04-amendments.md](04-amendments.md) | Parsing and applying izmena/dopuna/ispravka ops | `stage03b_apply_amendments.py` |
| 5 | [05-street-resolution.md](05-street-resolution.md) | Resolving a street name to a register street id: settlement‑first scope, exact/declension/sortkey/strip‑ulica/parts/fuzzy/alias/settlement‑claim ladder | `stage04_match_addresses.py` (`resolve_street`) |
| 6 | [06-claim-resolution.md](06-claim-resolution.md) | Assigning real register houses to stations: specificity, parity validation, suffix‑bounded ranges, bare‑implies‑suffix, conflicts, `бб` | `stage04_match_addresses.py` (`resolve_street_claims`) |
| 7 | [07-review-flags.md](07-review-flags.md) | Which segments get flagged `needs_review` and the reason codes | `stage04_match_addresses.py` (finalize) |
| 8 | [08-worker-live-preview.md](08-worker-live-preview.md) | The Worker's TypeScript mirror of matching + override resolution | `worker/src/db.ts` |
| 9 | [09-volunteer-mapping.md](09-volunteer-mapping.md) | Mapping volunteer GeoJSON files to register municipalities (filename heuristic, district splits, Palilula collision, child‑GO fold) and the geometry‑based comparison against automated polygons | `map_volunteer_polygons.py`, `compare_volunteer.py` |

## Conventions used in these docs

- **Normalized form** means the output of `normalize_street()` unless stated otherwise:
  NFC, uppercase Cyrillic, abbreviations expanded, Roman/ordinals folded to Arabic,
  punctuation dropped, whitespace collapsed. Examples are shown in normalized (UPPERCASE
  Cyrillic) form when illustrating a match key.
- **Settlement scope / muni scope.** Matching is *settlement‑first*: a station's home
  settlement (parsed from its address, or — for town stations with no address prefix —
  inferred as the eponymous town settlement, see [05](05-street-resolution.md) §5.1.1) is the
  default scope, with the municipality as fallback. Many rules apply ONLY in settlement scope
  — that distinction is load‑bearing and is called out per rule.
- **group_rep.** Cities are split into city‑municipalities in the register. Matching scope
  is keyed by the *group representative* municipality (`config.group_rep`) so a single city
  document resolves streets across all its members. Where a rule says "municipality" it
  means the group rep.
- Code references are `file:symbol` or `file:line`. Line numbers drift — the symbol name is
  the durable anchor.
</content>
</invoke>
