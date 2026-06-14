# Volunteer polygon mapping & comparison

A set of volunteer‑drawn polling‑station coverage polygons lives in
`data/volunteer-polygons/*.geojson`. They are an independent, hand‑drawn reference we use to
**QA** our automated (Voronoi + review) polygons, and the seed of a future ingest path where a
volunteer polygon could become the authoritative coverage for a station.

This document specifies (a) how a volunteer file is mapped to a register municipality despite
inconsistent naming, and (b) how the two polygon sets are compared. Code:
`pipeline/map_volunteer_polygons.py` (mapping) and `pipeline/compare_volunteer.py` (comparison).
Unit tests: `pipeline/tests/test_volunteer_mapping.py`.

## 1. Filename → municipality

Files are named `<MUNICIPALITY>_2023.geojson` in **Latin** script with Serbian diacritics, but
the naming does not line up cleanly with `municipalities.parquet`. The resolver
(`map_volunteer_polygons.resolve`) handles three wrinkles:

### 1.1 Normalization

Strip the `_2023` suffix, then normalize: NFC → `_`→space → collapse whitespace → uppercase.
Python `str.upper()` folds the Latin digraphs correctly, so `VRANjE` → `VRANJE`,
`KRALjEVO` → `KRALJEVO`, `PRIJEPOLjE` → `PRIJEPOLJE`. Diacritics (`Č Š Ž Ć Đ`) match the
register's `name_lat` as‑is. The register side is normalized the same way, and we match on the
**base** name with any parenthetical disambiguator stripped (`PALILULA (NIŠ)` → base `PALILULA`).

### 1.2 City‑district split files

Files using a `_-_` (Niš, Požarevac) or bare `-` (Užice, Vranje) separator carry a
`CITY - DISTRICT` name (`split_prefix` returns `(prefix, core)`):

| File | prefix | core | resolves to |
|---|---|---|---|
| `NIŠ_-_MEDIJANA` | NIŠ | MEDIJANA | MEDIJANA (71331) |
| `POŽAREVAC_-_POŽAREVAC` | POŽAREVAC | POŽAREVAC | POŽAREVAC (70947) — self‑named → parent city |
| `UŽICE-SEVOJNO` | UŽICE | SEVOJNO | SEVOJNO (71366) |

The `core` is what we resolve; the `prefix` only disambiguates collisions (§1.3).

### 1.3 The Palilula collision

`PALILULA` exists twice in the register: `PALILULA (BEOGRAD)` (70203) and `PALILULA (NIŠ)`
(71323). When a base name maps to >1 municipality:

- if there is a city `prefix`, pick the candidate whose parenthetical matches the prefix —
  `NIŠ_-_PALILULA` → `PALILULA (NIŠ)`;
- otherwise (bare ambiguous name) fall back to the `(BEOGRAD)` candidate — `PALILULA` →
  `PALILULA (BEOGRAD)`, since the bare district name refers to the Belgrade city‑municipality.

### 1.4 `polygon_muni_id`: child districts fold into the parent

Three districts — **Kostolac (71340 → Požarevac 70947)**, **Sevojno (71366 → Užice 71145)**,
**Vranjska Banja (71358 → Vranje 70432)** — have a `parent_id` and **no automated polygons of
their own**: the pipeline files their stations under the parent city. So the resolver records two
ids: `muni_id` (the true register municipality) and `polygon_muni_id` (where the automated
polygons actually live — the parent when the resolved muni has no R2 polygon file). The
comparison groups by `polygon_muni_id`, so the Kostolac and Požarevac volunteer files are compared
together against muni 70947.

### 1.5 Output: `mapping.csv`

`artifacts/volunteer-compare/mapping.csv` (columns: `file, prefix, core, muni_id, muni_name_lat,
polygon_muni_id, n_features, n_with_brbm, method, status`). `status` is one of `ok`,
`ambiguous_resolved`, `child_go_merged_to_parent`, `no_polygons`, `unmatched`. The CSV is
hand‑editable and is the contract the comparison reads — correcting a mis‑map is a one‑line edit.

As of the 2023 volunteer set, all 77 files resolve: 72 `ok`, 2 `ambiguous_resolved` (the two
Palilulas), 3 `child_go_merged_to_parent`; zero `unmatched`.

## 2. Comparison

`compare_volunteer.py` groups volunteer files by `polygon_muni_id` and compares each group against
the parent muni's R2 polygon file (`artifacts/r2/polygons/m/<id>.json`). All area/containment math
is in **UTM34N (EPSG:32634)**, the register‑native metric CRS.

### 2.1 CRS detection

Most files are WGS84 lat/lon, but a few were exported in **UTM34N metres** (e.g. KNJAŽEVAC). A
coordinate whose magnitude exceeds 200 cannot be a degree, so `_file_is_utm` treats such a file as
already projected (used directly as UTM; inverse‑transformed for the WGS84 overlay). Null and
empty geometries are skipped.

### 2.2 Geometry‑based matching

The per‑station identifier is **inconsistent**: ~68 files carry a real `BR_BM` polling‑station
number, but seven (KNIĆ, KNJAŽEVAC, KOSJERIĆ, LUČANI, PIROT, SMEDEREVO, SUBOTICA) have only a
global `RBR`/`id` with empty `puno_ime`. So alignment is **geometric, not by number**:
`match_geometric` builds candidate pairs from an `STRtree` intersection query, sorts all
overlapping (auto, volunteer) pairs by intersection area descending, and greedily assigns each
side at most once. A volunteer "unit" dissolves features sharing a `BR_BM`; number‑less features
each stand alone (one polygon per station, as drawn). Where `BR_BM` exists we additionally record
whether the geometric match agrees with the number, as a cross‑check column.

### 2.3 Metrics & outputs

Per matched pair: IoU, area ratio, asymmetric coverage (of volunteer / of automated), centroid
distance, and a `cause_tag`. Per group: matched/unmatched counts, IoU mean/median/histogram, total
area ratio, and per‑address accuracy — *containment* (does an auto‑assigned address land inside the
volunteer unit matched to its station?) and *agreement* (is the unit containing an address the one
matched to the address's automated station?). Outputs:

- `artifacts/volunteer-compare/summary.md` — all municipalities ranked by mean IoU (worst first);
- `artifacts/volunteer-compare/m/<polygon_muni_id>/report.md` + `overlay.geojson` + `overlay.html`
  (a standalone Leaflet overlay, blue = automated, red dashed = volunteer).

### 2.4 Reading the results

Low IoU with high containment is the common pattern: volunteers draw to administrative boundaries
(including uninhabited land) while the automated polygons are Voronoi cells clipped to actual
address points, so the automated area is typically smaller (`area_ratio < 1`). A *mislocated*
volunteer file shows up as 0 matches / 0 containment at the top of the ranking — e.g. `KNIĆ` and
`MIONICA` in the 2023 set carry coordinates far from the named municipality and should be treated
as bad source data, not pipeline error.
