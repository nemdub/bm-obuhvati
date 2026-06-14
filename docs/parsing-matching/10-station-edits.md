# Station-level edits: add / remove stations & fix source text

Segment-level review (edit a segment's coverage, reassign its street, add a missing street to a
station) is covered by [08-worker-live-preview.md](08-worker-live-preview.md). This document
covers the three **station-level** reviewer actions added on top of it, when stage02's
extraction is wrong at the granularity of a whole station:

1. **Fix the raw coverage source text** of an existing station and have it re-parsed.
2. **Add a brand-new station** the RIK document dropped entirely (coverage entered as raw text).
3. **Remove a station** that should not exist (duplicate / mis-parsed), reversibly.

All three follow the project's established pattern: **worker-owned D1 tables** (which survive
derived re-imports) → exported by `fetch_overrides.sh` → consumed by the pipeline → fed through
the normal stage04→06 delta machinery. The reviewer UI lives on the existing municipality and
station pages (`worker/src/views.ts`, `worker/public/{muni,app}.js`); the API is in
`worker/src/index.ts`; the worker reads are in `worker/src/db.ts`.

## 1. Worker-owned tables (migration `0011_station_edits.sql`)

| Table | Written by | Holds |
|-------|-----------|-------|
| `station_text_overrides` | `PUT /api/s/:id/text` | corrected `raw_coverage_text` for an existing station |
| `added_stations` | `POST /api/m/:id/stations` | brand-new stations (muni, number, name_cyr, address_cyr, raw_coverage_text) |
| `removed_stations` | `POST /api/s/:id/remove` | tombstones (station_id + reason) |

**Synthetic station id.** An added station gets `id = ADDED_STATION_BASE + added_stations.id`
where `ADDED_STATION_BASE = 9_500_000_000_000` (shared constant in `worker/src/db.ts` and
`pipeline/config.py`). This sits above the segment-claim space (`ADDED_SEG_BASE = 9e12`) and far
above real ids (`municipality_id * 100000 + n`), so the three id spaces never collide. Its
segment ids stay `station_id * 1000 + idx` off the synthetic id, like any station.

## 2. Worker behaviour

- **Reads (`db.ts`).** `getStation` synthesizes a `StationRow` for a synthetic id from
  `added_stations`, and overlays `station_text_overrides.raw_coverage_text` (COALESCE) for real
  stations. `listStations` appends added stations not yet present as real rows (deduped by id,
  exactly like added segments in `getSegments`) and flags tombstoned rows `removed`. Removed
  stations are excluded from the map/export routes via `removedStationIds` so they disappear
  immediately, before their R2 polygon blob is rebuilt.
- **Text fix purges segment overrides.** Re-parsing renumbers a station's positional segment ids
  (`station_id*1000+idx`), so `PUT /api/s/:id/text` also deletes that station's
  `segment_overrides` rows — otherwise they would misapply to different segments after the
  re-parse. The UI warns about this. (Same hazard as a parser change; see the pipeline notes.)
- **Pre-recompute state.** An added or text-fixed station shows its raw text immediately but has
  no points/polygon until the next recompute parses it — consistent with how any freshly-edited
  station behaves (it is marked `dirty`).

## 3. Pipeline: `stage03c_reconcile_edits.py`

`recompute.sh` runs only stages 04–06 and deliberately skips parsing, but added stations and
corrected text both need the parser. So a reconcile step runs at the front of the recompute
(after `fetch_overrides.sh`, before stage04). It re-uses stage03's parser
(`segments_for_station`) so corrected/new text parses identically to base extraction, and
rebuilds the canonical `stations.parquet` + `segments_amended.parquet`:

- **text override** → replace the station's `raw_coverage_text`, drop its old segments, re-parse;
- **added station** → inject a station row (`name_lat`/`address_lat` via `cyr_to_lat`,
  `source_file='manual'`) + its parsed segments;
- **removed station** → drop it from both parquets — stage06's delta diff then emits the
  `DELETE FROM coverage_segments / polling_stations` automatically.

### 3.1 Pristine snapshots make revert/restore work

The reconcile **always rebuilds from pristine, edit-free snapshots**
(`stations_pristine.parquet`, `segments_amended_pristine.parquet`) rather than mutating the
canonical parquets cumulatively. This is what lets a reverted text fix or a restored station
recover **without** a full re-parse: the edit is simply absent from the next run's input, so the
rebuilt canonical reflects the pristine value again. The snapshots are refreshed by **stage03b**
at the end of every full rebuild, and bootstrapped by stage03c from the canonical parquets if
absent. Mutating the canonical parquets in place would strand the original text/rows until the
next full stage01–03 run.

### 3.2 Scope resolution (`dirty_scope.py`)

Every station-level edit calls `markDirty`, so the dirty snapshot drives an incremental
recompute as usual. `dirty_scope.py` resolves station → municipality from the **pristine**
snapshot (so a removed station — already dropped from canonical — still maps to its muni and
stays in scope; its muni must be re-tessellated to drop it from the R2 blob) and merges
`added_stations.json` (whose synthetic ids are in no parquet yet).

## 4. Data flow

```
muni / station page (worker)
   PUT /api/s/:id/text · POST /api/m/:id/stations · POST /api/s/:id/remove (+ DELETE reverts)
        ↓                    ↓                         ↓
station_text_overrides   added_stations           removed_stations         (D1, worker-owned)
        └────────────────────┴── fetch_overrides.sh ──┴──▶ text_overrides.json / added_stations.json / removed_stations.json
                                                              ↓ stage03c_reconcile_edits.py (rebuild canonical from pristine)
                                          stations.parquet + segments_amended.parquet
                                                              ↓ stage04 → stage05 → stage06 (delta: INSERT added / DELETE removed)
                                                          D1 derived rows + R2 polygon blobs
```

Code: `worker/migrations/0011_station_edits.sql`, `worker/src/{index,db,views,i18n}.ts`,
`worker/public/{app,muni}.js`, `pipeline/stage03c_reconcile_edits.py`,
`pipeline/{recompute.sh,fetch_overrides.sh,dirty_scope.py,config.py}`,
`pipeline/stage03b_apply_amendments.py` (pristine refresh). Tests:
`pipeline/tests/test_station_edits.py`.
