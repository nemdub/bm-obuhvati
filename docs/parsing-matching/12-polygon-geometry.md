# 12 — Polygon geometry (stage05)

Unlike the other documents here (which cover parsing and matching), this one covers the
**geometry** stage: how the matched address→station links become the per‑station coverage
polygons served on the maps. Primary code: `pipeline/stage05_voronoi.py`; tests
`pipeline/tests/test_voronoi.py`.

## 12.1 How a polygon is built

For each settlement the pipeline computes a **Voronoi diagram** over its matched address points
(UTM, meters) and gives each station its **full Voronoi territory within the settlement**: the
station's cells are `unary_union`‑ed and **clipped to the official naselje boundary**
(`settlement_geometry.parquet`, 100 % address‑settlement coverage). Because Voronoi cells tile
space, the per‑station regions are disjoint and together fill the settlement — contiguous simple
shapes instead of many tiny per‑address circles — and can never cover another station's address
(§12.2). Territories are then unioned across settlements (for spanning stations), **clipped to
the municipality boundary**, despiked, hole‑cleaned (§12.3) and simplified to WGS84 GeoJSON.

Two safeguards keep coverage complete:
- **Halo constraints.** Foreign points (other settlements) within the settlement's bbox
  participate in the Voronoi as constraints (their cells discarded), so where two naselje
  boundaries overlap a territory stops at the bisector before reaching a neighbour's address.
- **Outlier caps.** ~0.15 % of addresses sit *outside* their own naselje boundary (register vs
  boundary‑source disagreement). Such an address keeps a tight capped Voronoi cell
  (`POLYGON_CELL_CAP_M`) instead of being clipped away, so no listed address loses coverage.

Whole‑settlement claims and OSM‑fallback shapes are unioned into the station's geometry before
the generalize step (see [05](05-street-resolution.md) §5.12–5.16).

## 12.2 The non‑overlap invariant

The project's whole point is knowing **which addresses belong to which station**, so a polygon
must **never cover another station's matched address** unless both stations genuinely claim it.
The Voronoi tessellation already guarantees this for the address cells: each cell is capped and
then **intersected with that address's Voronoi cell, which tiles space**, so a cell can never
reach an address that belongs to a different station (measured baseline: of 27 747 cross‑station
address containments only **73** come from address cells — the rest are whole‑settlement / OSM
claims, which cover a whole area *by design*). Any post‑processing must preserve this.

**Simplification that fills empty land must respect the tessellation.** An early attempt used an
aggressive morphological *close* to merge a sparse station's scattered per‑address octagons into
hull‑like blobs. It looked simpler but the bridges ran across land — and addresses — belonging to
neighbours: cross‑station containments exploded **27 747 → 350 496**. The lesson: filling must
follow the Voronoi partition, not a blind buffer. The **territory fill** (§12.1) does exactly
that — each station gets its Voronoi region clipped to the naselje boundary, which by construction
tiles the settlement without overlap. Measured full run vs the old 180 m circles: **parts
26 822 → 18 995** (contiguous shapes), cross‑station overlap **73 → 73** (unchanged — the halo +
outlier safeguards hold it at the baseline level, with **zero** cross‑settlement overlap), own
addresses uncovered 171 → 216 (both ~0.01 %), 0 invalid, max blob 41 436 B (< 45 KB).

## 12.3 Overlap‑safe hole removal

The one safe cleanup is removing the **tiny interior gaps** the octagons leave when they don't
tile a village perfectly. After the union + the existing despike close‑then‑open, stage05 runs
`drop_small_holes(geom, POLYGON_MIN_HOLE_M2, keep_ring=…)`:

- Drop an interior ring only if it is **< `POLYGON_MIN_HOLE_M2` = 5 000 m²** **and** it encloses
  **no other station's matched address** (checked with `_osm_foreign_overlap` over the coarse
  point grid — the same helper the OSM‑claim guard uses).
- **Keep** a ring that is large (a genuine enclave) **or** that contains a foreign address —
  filling the latter would swallow that address and break the invariant.

This is provably overlap‑neutral: dropping a ring only adds *empty* land to the polygon.

### Measured effect (full run, 7 885 stations)

small holes (< 5 000 m²) 5 659 → **~983** (the removed ones are empty tiling gaps; the kept ones
each enclose a neighbour's address); total holes 7 783 → ~3 000; **cross‑station overlap unchanged
at the baseline 73 plain‑cell**; 0 invalid geometries.

## 12.4 Output & simplification

The polygon goes through the existing **adaptive simplify** loop — escalate the tolerance
`(5, 15, 40, 100, 250)` m until the GeoJSON fits the ~45 KB budget — then is written to
`polygons.parquet` and, in stage06, to the per‑municipality R2 blobs `polygons/m/<muni>.json`.
The reprojected geometry is validity‑repaired (`.buffer(0)` when `is_valid` is false — a
topology‑only op) so the output stays 100 % OGC‑valid. This is a **stage05‑only** change: no
schema, D1, or Worker‑logic change; the shapes just change.

## 12.5 Reviewing before/after in the map

To compare iterations in the existing Worker map without disturbing the live shapes, the
candidate blobs are published to a **second R2 key prefix** and the two map endpoints select a
variant via `?v=`:

- **Publish candidate:** `R2_KEY_PREFIX=polygons-v2 ./sync_r2.sh` (see `pipeline/sync_r2.sh`) —
  leaves the live `polygons/` prefix untouched.
- **Worker:** `polyVariant(?v)` maps `current`→`polygons/`, `candidate`→`polygons-v2/`
  (`worker/src/db.ts`); `/api/m/:id/polygons.geojson` and `/api/s/:id/polygon.geojson` honor it.
  Unknown/missing `v` = `current`, so normal traffic is unchanged.
- **Map UI:** a top‑right toggle (`Current / Simplified`) on both the municipality overview
  (`worker/public/muni.js`) and station (`worker/public/app.js`) maps re‑fetches and swaps the
  polygon layer **without moving the viewport**; the choice persists in `localStorage`.

Promote by republishing the candidate to the default `polygons/` prefix once approved.
