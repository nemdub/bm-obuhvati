# bm-obuhvati

Generates geographic **coverage polygons** for every polling station ("biračko mesto")
in Serbia, and serves a web tool to review and correct them.

Serbia's Republic Electoral Commission (RIK) defines each polling station's coverage as
free text inside per-municipality Word documents. This project parses that text, matches
it to the official address register (which has coordinates), builds a per-station coverage
polygon via Voronoi tessellation, and exposes everything in a Serbian-language review UI
(Latin/Cyrillic toggle) backed by Cloudflare D1.

- **Live app:** https://bm-obuhvati.dubravac-nemanja.workers.dev
- Code and documentation are in English; the UI is in Serbian.

## Architecture

Two halves:

1. **`pipeline/`** — offline Python ETL (run locally). Loads + reprojects the address
   register, extracts the RIK documents, parses coverage, matches addresses, builds
   Voronoi polygons, and emits a SQLite DB + SQL dumps for D1.
2. **`worker/`** — a Cloudflare Worker (Hono, server-rendered) reading D1, with a Leaflet
   map review UI. The Worker never re-parses or re-tessellates; heavy recompute stays in
   Python (the Worker flags a station `dirty`; a future `--only-dirty` pipeline run
   consumes it).

### Data sources (under `data/`, not version-controlled, ~758 MB)

- `data/kucni_broj.csv` — address register, 2,484,921 rows. Street/house number (Cyrillic
  + Latin), settlement/municipality ids, `wkt` = `POINT(x y)` in **UTM Zone 34N
  (EPSG:32634)**, reprojected to WGS84 for mapping.
- `data/polling_stations_2022/` — 211 MS Word files (168 `.doc`, 43 `.docx`), one per
  municipality, ~46 amendment files. Downloaded via `scrape_polling_stations.py`.

## Pipeline

Requires macOS (`textutil` for Word extraction) and the `.venv`:

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -e pipeline   # or install deps from pipeline/pyproject.toml
cd pipeline
../.venv/bin/python stage01_load_register.py        # CSV -> parquet, reproject (+ reference tables)
../.venv/bin/python stage02_extract_docs.py         # Word docs -> stations + raw amendments
../.venv/bin/python stage03_parse_coverage.py       # coverage text -> segments
../.venv/bin/python stage03b_apply_amendments.py    # apply amendment ops to segments
../.venv/bin/python stage04_match_addresses.py      # resolve streets + match register addresses
../.venv/bin/python stage05_voronoi.py              # per-settlement Voronoi -> station polygons
../.venv/bin/python stage06_build_sqlite.py         # assemble bm.sqlite + import_*.sql
```

Stage 01 and `--municipalities "ADA,BOR,SUBOTICA"` (stage01) / `--files` (stage02) /
`--municipality <id>` (stage03) flags produce a fast dev subset.

Outputs land in `pipeline/artifacts/` (gitignored): `bm.sqlite` (canonical) and
`import_reference.sql`, `import_addresses.sql`, `import_derived.sql`.

### Key design points

- **Cyrillic matching.** Streets/houses are normalized and matched in Cyrillic;
  `190Б` / `190-Б` / `190B` collapse to `(190, "Б")`.
- **Ranges resolve against the real register**, never synthesized: `13-61` selects the
  register house numbers on that street with `13 ≤ num ≤ 61`.
- **One address → one station**, so the Voronoi tessellation is gap-free / non-overlapping.
  Unmatched points tessellate as "unassigned" to expose coverage gaps.
- **Re-run safety.** A segment's effective coverage is `COALESCE(manual_json,
  parsed_json)`. The pipeline only writes `parsed_json`; human edits live in `manual_json`
  and are never overwritten.

## Worker

```bash
cd worker
npm install
npm run migrate:local                                # apply schema to local D1
# load data into LOCAL D1 (order matters for FKs):
npx wrangler d1 execute bm-obuhvati --local --file=../pipeline/artifacts/import_reference.sql
npx wrangler d1 execute bm-obuhvati --local --file=../pipeline/artifacts/import_addresses.sql
npx wrangler d1 execute bm-obuhvati --local --file=../pipeline/artifacts/import_derived.sql
npm run dev                                           # http://localhost:8787
```

Deploy + remote data load:

```bash
npm run deploy
npm run migrate:remote
npx wrangler d1 execute bm-obuhvati --remote --file=../pipeline/artifacts/import_reference.sql
npx wrangler d1 execute bm-obuhvati --remote --file=../pipeline/artifacts/import_addresses.sql
npx wrangler d1 execute bm-obuhvati --remote --file=../pipeline/artifacts/import_derived.sql
```

Reference + addresses are insert-only one-time loads. `import_derived.sql` (stations,
segments, amendments, links, polygons) deletes + reloads, so it is safe to re-run after a
fresh pipeline pass without touching the 2.48 M addresses.

> Note: this `wrangler` (v4) has no `d1 import`; use `d1 execute --file`. The SQL is
> batched and capped at ~50 KB per statement to stay under D1's statement-size limit.

### Review → recompute loop

Reviewer edits (number/parity changes, manual street assignments, "mark reviewed") are
stored in the `segment_overrides` D1 table — they survive data re-imports and reflect
immediately in the live map points. To fold them into the stored **polygons**:

```bash
pipeline/recompute.sh        # fetch overrides -> stage04-06 -> import derived to remote D1
```

Flags: `--no-fetch` (reuse existing `artifacts/overrides.json`), `--no-import` (rebuild
locally only). Takes ~5 minutes; touches only the derived tables, so it is safe to re-run.

### Review UI

`/` municipalities → `/m/:id` stations (worst-first by `needs_review`) → `/s/:id` station
detail: raw coverage text, an editable segment list (whole-street toggle, ranges, single
numbers; save / revert-to-machine / mark-reviewed), and a Leaflet map showing matched
address points (colored by confidence) and the coverage polygon with neighbors for
context. `?script=lat|cyr` toggles the script (cookie-persisted).

A street the parser couldn't match can be (re)assigned to a register street via the
per-segment street picker, or — when it genuinely isn't in the register — marked
**"Doesn't exist"**. That resolves the segment (out of the review queue) and stores the
`"none"` sentinel in `segment_overrides.manual_street_id` so no addresses/polygon are
built for it; the pipeline honors the same sentinel on recompute (drops any machine
match). It is revertable like any manual edit.

## Known limitations / where review concentrates

1. **Amendment formats.** Only the Subotica-style amendment phrasing is parsed; the other
   ~45 amendment documents use different wording and are not yet applied (base coverage is
   still loaded). Amendment parsing is the main area for iteration.
2. **Compact-dialect commas** (street vs. number) and **street-name fuzzy resolution**
   (abbreviations/typos, settlement-name collisions within a municipality) are the top
   sources of parse error — all flagged via `needs_review`.
3. **Number-less / restart-numbered documents** (some cities) use a triplet fallback and a
   per-municipality running index; a few over-segment (flagged).
4. The settlement boundary used to clip Voronoi cells is a buffered convex hull; swap in
   official boundaries later (it is a pluggable input in `stage05`).
