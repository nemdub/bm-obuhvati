# Volunteer + automated polygon combine (standalone export)

A **one-off side task**, not part of the pipeline. It fuses the hand-drawn volunteer coverage
polygons with our automated (Voronoi) ones into a **separate combined dataset** — volunteer-primary,
automated-fallback — for reuse in other projects. It does **not** touch the main pipeline, the D1/R2
serving path, or the Worker UI. The automated pipeline continues to produce automated-only polygons.

Where [09](09-volunteer-mapping.md) *maps and compares* the two polygon sets, this document *combines*
them. Code: `pipeline/clean_volunteer_data.py` (normalize + QA), `pipeline/combine_volunteer.py`
(merge), `pipeline/fetch_prod_polygons.sh` (production automated blobs). Tests:
`pipeline/tests/test_volunteer_combine.py`.

## 0. Inputs

`data/volunteer-data/` holds, per municipality, up to three files (this **supersedes** the older
`data/volunteer-polygons/`):

| Family | Filename | Script | Fields |
|---|---|---|---|
| coverage polygons | `<NAME>_2023.geojson` | Latin | `puno_ime` = `"<N> - <NAME>"`; `BR_BM` (74/81 files) |
| voter/priority table | `<NAME_CYR>_biracka_mesta.xlsx` | Cyrillic | Broj BM, Naziv, Adresa, Upisani birači, Prioritet (1–9) |
| station points | `<NAME_CYR>_lokacije.kmz` | Cyrillic | one point/station, priority, ExtendedData (br_bm/adresa/biraci/prioritet) |

The automated side is the per-muni R2 blob (`{"stations":[…]}`), fetched from **production** first
(`fetch_prod_polygons.sh` → `artifacts/prod-r2/polygons/m/<id>.json`) so the merge runs against the
live polygons rather than a possibly stale local `artifacts/r2/…`. It pulls **all** munis (the id list
is the local stage06 blob set), because the combined dataset covers every municipality — not only the
volunteer ones.

## 1. Cleanup / normalization (`clean_volunteer_data.py`)

Per geojson file, resolved to a municipality with `map_volunteer_polygons.resolve`:

- **geojson → units.** Reuse `compare_volunteer` geometry helpers: per-file CRS detection
  (`_file_is_utm` — a coordinate magnitude > 200 means UTM34N, not degrees), `make_valid` repair,
  dissolve features sharing a station number, skip null/empty geometries. Station number =
  `BR_BM`, else the leading integer of `puno_ime` (`unit_number`).
- **xlsx.** Strings are stored inline (`<c t="str"><v>…</v></c>`, no `sharedStrings`), so
  `parse_sheet_xml` reads `xl/worksheets/sheet1.xml` with `lxml` directly — no `openpyxl`.
- **kmz.** `parse_kml` reads `doc.kml`, preferring the structured `<ExtendedData>` (br_bm / adresa /
  biraci / prioritet), falling back to the description text (`"…br. N"`, Latin or Cyrillic) and the
  `styleUrl` (`#novaPriN`) for priority.
- **Number rescue.** The 7 files with neither `BR_BM` nor `puno_ime` (KNIĆ, KNjAŽEVAC, KOSJERIĆ,
  LUČANI, PIROT, SMEDEREVO, SUBOTICA) all have a KMZ, so `rescue_numbers` assigns each number-less
  drawn unit the number of the KMZ point it contains (point-in-polygon, UTM).
- **Filename join.** Latin geojson names vs Cyrillic xlsx/kmz names collapse via
  `companion_key = _norm(cyr_to_lat(stem_without_suffix))` (transliteration is identity on the
  already-Latin geojson stem).
- **Priority (1-9) is combined from all three sources** (`select_priority`), because it lives in
  different places across the files: on the **geojson polygon** itself (`prioritet`, a few files —
  Vranje, Novi Pazar, Požarevac), in the **kmz** for the station point, and/or in the **xlsx**. Per
  unit the most specific wins: geojson polygon → kmz station → xlsx (recorded as `priority_source`).
  The DQ report breaks down how many units drew from each.
- **Station table** (`station_table`) — a second, polygon-INDEPENDENT product: the volunteer station
  list keyed by the Excel `Broj BM` (name/address/voters/priority). It exists so a municipality whose
  polygons were dropped (wrong scheme / RBR) — or which never had polygons at all — can still
  contribute its priority/voters: the combine joins it onto the *automated* polygons by station number
  (see §2). The kmz is only consulted where its point number IS a valid `Broj BM`, so an RBR-numbered
  kmz (Lučani) simply misses and the Excel value wins.
- **Excel-only municipalities.** Most munis were surveyed with a station list but **no drawn
  polygons** (168 xlsx vs 81 geojson). `main()` therefore does a second pass over every xlsx with no
  geojson companion, resolving it with `resolve_xlsx_muni` (`cyr_to_lat` + the existing muni resolver
  — all 168 resolve, 0 unmatched) and emitting just its station table. They have no polygons, so they
  correctly stay `source=automatic`. Their dq entry carries `has_geojson=False`, and the
  geometry/number findings are gated on that flag so they aren't falsely reported as "no BR_BM".
- **Address verification** (`address_key` / `same_address`) — the guard that makes the by-number
  attach safe; see §2.

Output: `artifacts/volunteer-combined/normalized/<polygon_muni_id>.json` (unit records with
number, name, address, voters, priority, point, WGS84 geometry, area) plus
`artifacts/volunteer-combined/data_quality_report.md` — the "note the gaps" deliverable: null
geometries, missing BR_BM (+ whether rescued), munis without a KMZ, geojson/xlsx/kmz count
mismatches, geocoding shortfalls, and UTM-CRS files.

## 2. Merge (`combine_volunteer.py`) — WHOLE-MUNICIPALITY, single source

Covers **every municipality we have an automated polygon blob for** (~165), driven by the blob list in
`artifacts/prod-r2/polygons/m/` — not just the volunteer ones. **Sources are never mixed within a
municipality:** each muni is emitted entirely from volunteer OR entirely from automated. (The earlier
version merged per-station; the user decided against mixing.)

**Per-file usability guard** (`unusable_files`) auto-detects mislocated / wrong-CRS files: a volunteer
file whose drawn area overlaps the automated union by less than `VOL_FILE_MIN_OVERLAP_FRAC` (5%) is
dropped as mislocated. `config.VOLUNTEER_UNUSABLE_FILES` is a manual escape hatch. (MIONICA is caught
this way; KNIĆ — mislocated in the old drop — is fine in the new one, so the guard beats hardcoding.)

**A volunteer polygon is only usable if we can trust which station it is** — the authoritative station
numbers are the Excel `Broj BM` (1..N). A unit is dropped when:
- its file is **mislocated** (geometric guard, MIONICA); or
- its geojson uses a **wrong numbering scheme** — every feature carries the global **RBR** instead of
  the local `Broj BM` (`geojson_scheme_wrong`; SUBOTICA labels polygons 1256… not 1–102). The whole
  file is dropped with a precise reason; or
- **its resolved number is not a valid Excel `Broj BM`** — number-less, or an RBR that came in through
  the KMZ rescue (LUČANI/KOSJERIĆ point-numbers are 5751…, not 1–43). These per-polygon drops catch the
  files whose geojson has no numbers but whose KMZ carries the RBR. The KMZ *number* field is therefore
  NOT trusted as ground truth — only the Excel `Broj BM` is.

**Per-municipality decision** (`decide_source`): use **volunteer** for the muni unless —
- **no volunteer data** for the muni at all → **automated** (the ~87 munis volunteers never covered); or
- no usable volunteer units remain — mislocated (MIONICA), wrong scheme (SUBOTICA), or every polygon
  RBR-numbered (LUČANI, KOSJERIĆ) → **automated**; or
- **incomplete**: `usable volunteer polygons / automated station count < VOL_MUNI_MIN_COMPLETENESS`
  (0.70) → **automated** (KNJAŽEVAC).

otherwise **volunteer**, emitting only the polygons with a valid `Broj BM` (so PIROT/SMEDEREVO ship 78%
/73% of stations, their RBR/unresolved polygons dropped). A muni is all one source. In the 2023 set:
165 munis → **73 volunteer**, **92 automated**. Every emitted feature carries a valid Excel `Broj BM`.

**Emit (single source per muni):**
- **volunteer muni** → the volunteer polygons **raw** (no register cross-matching): `source="volunteer"`,
  `station_id=null`, `BR_BM`/`NAZIV_BM`/`ADRESA_BM` = the volunteer's own number/name/address, plus
  `priority`, `voters`, `vol_br_bm`. Stations the volunteers didn't draw simply don't appear (the
  completeness guard keeps that below ~30%).
- **automated muni** → the automated polygons with register identity, like `export_automated_geojson`:
  `source="automatic"`, `station_id`, register `BR_BM`/`NAZIV_BM`/`ADRESA_BM`/`Uparivanje`. If the muni
  *was* surveyed, the volunteer **station table** is joined by `BR_BM` to fill `priority`/`voters`
  (geometry and identity stay 100% register — only the attributes are attached). Guarded: if two
  volunteer files in the muni reuse the same `Broj BM` (a merged parent), the join is refused.

  **Every such join is confirmed against the ADDRESS**, because the 2023 volunteer numbering has
  drifted from the current register in many municipalities — an inserted/removed station shifts every
  later `Broj BM` (Ćuprija has an extra station at #4, so register #5 = Excel #6; Rača lists 27 vs 22).
  Equal station counts are **not** proof: Leskovac is 194 vs 194 and still drifts by ~#100. Comparing
  names is also unreliable (Leskovac agrees on only 29% of names while the addresses are identical —
  "МШЦ" vs "ТЕХНИЧКА ШКОЛА"). `same_address` compares an order-insensitive token set
  (`address_key`, homoglyph-folded) so "Школска 8, Беочин" == "БЕОЧИН, ШКОЛСКА БР. 8", with a Jaccard
  (`VOL_ADDR_JACCARD_MIN`) and `rapidfuzz` (`VOL_ADDR_FUZZ_MIN`) fallback. An unconfirmed pair keeps
  null priority/voters and `priority_source="unverified"` — 774 stations nationwide. Without this
  guard ~700 stations would silently carry **another station's** priority.
- Both branches emit the **same property key set** (nulls where N/A) so the schema is uniform.

## 3. Outputs

- `data/volunteer-combined/<slug>_<muni>_combined.geojson` (+ `combined_all.geojson`) — the deliverable.
  Feature properties: `station_id, BR_BM, OPSTINA, NAZIV_BM, ADRESA_BM, Uparivanje, source, priority,
  voters, vol_br_bm`. Geometry is WGS84. **Each file is single-source.**
- `artifacts/volunteer-combined/combine_report.md` + `combine_stats.json` — per muni the chosen source,
  the automated-fallback reason, feature count, and completeness.
- `artifacts/volunteer-combined/m/<id>/overlay.html` — a Leaflet overlay (green = volunteer,
  blue dashed = automated) for eyeballing; serve the dir with any static server.
- `data/volunteer-combined/SUMMARY.md` — the **per-municipality data report**, built by
  `pipeline/volunteer_combine_summary.py` from `mapping.csv` + `data_quality.json` +
  `combine_stats.json`. One row per muni (polygon source, polygons/register stations, priority
  availability, voter availability, discrepancies) plus sections documenting the **merged
  municipalities** (Sevojno→Užice, Kostolac→Požarevac, Vranjska Banja→Vranje — they get no row of
  their own), the **data mapping issues** (Subotica's RBR-numbered geojson …), the **coverage gaps**
  (register stations with no polygon, e.g. Mionica 5/38), and the remaining volunteer data-quality
  issues.
- `artifacts/volunteer-combined/stations/<polygon_muni_id>.json` — the volunteer **station table**
  (Broj BM → name/address/voters/priority), built from the Excel and independent of any polygon, so
  it survives the polygon drops (see §2).

## 4. Running it

```bash
.venv/bin/python pipeline/map_volunteer_polygons.py   # refresh mapping.csv for the new data dir
sh pipeline/fetch_prod_polygons.sh                    # pull production automated blobs
.venv/bin/python pipeline/clean_volunteer_data.py     # normalized records + data_quality_report.md
.venv/bin/python pipeline/combine_volunteer.py --source prod   # combined GeoJSON + combine_report.md
.venv/bin/python pipeline/volunteer_combine_summary.py         # per-municipality SUMMARY.md
.venv/bin/python pipeline/build_combined_viewer.py             # review web page (index.html + manifest.json)
cd data/volunteer-combined && python3 -m http.server 8791      # open http://localhost:8791/ to sweep all 78
```

`build_combined_viewer.py` writes a self-contained Leaflet page (`data/volunteer-combined/index.html`)
to eyeball every combined file: a sidebar of municipalities each tagged VOL/AUTO with its polygon count
and cleanup warnings, ←/→ navigation, source-coloured polygons, and a checked/flagged tracker persisted
in the browser so a full sweep can be resumed.

As of the 2023 volunteer set (168 xlsx / 81 geojson / 77 kmz): **165 municipalities** combined (all of
Serbia) → **73 use volunteer** polygons (4,480), **92 use automated** (3,418 — mostly munis surveyed
without drawn polygons, plus MIONICA mislocated, SUBOTICA wrong-scheme, LUČANI/KOSJERIĆ/KNJAŽEVAC
RBR-numbered/incomplete); **7,898 features** total, every one carrying a valid Excel `Broj BM`.
**Priority/voters on 7,099** of them across **162** municipalities; **774** left null as unverified
(numbering drift), and 3 munis (Kučevo, Lapovo, Žabari) end up with none.
