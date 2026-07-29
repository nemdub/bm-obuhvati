# 12 — Polygon geometry (stage05)

Unlike the other documents here (which cover parsing and matching), this one covers the
**geometry** stage: how the matched address→station links become the per‑station coverage
polygons served on the maps. Primary code: `pipeline/stage05_voronoi.py`; tests
`pipeline/tests/test_voronoi.py`.

## 12.1 How a polygon is built

For each settlement the pipeline computes a **Voronoi diagram** over its **matched** address points
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

**Only MATCHED addresses seed the tessellation.** An unmatched address (its street didn't resolve)
used to seed a cell that was then *discarded*, punching that whole Voronoi cell out of the station
around it. That single detail produced most of the ragged geometry: the corridors that split a
village station into islands, the bays along its edge, and the bulk of the holes. Ignoring those
points lets the matched cells tile the settlement. Non‑overlap is untouched — it is still a Voronoi
over matched points, so a cell can never reach another station's matched address.

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

## 12.3 Closing unclaimed holes

A station's territory comes out riddled with holes, from two sources: the **tiny gaps** the cells
leave where they don't tile perfectly, and — much larger — the **discarded Voronoi cells of
UNASSIGNED addresses**. The second one is the interesting case: unmatched addresses still seed the
tessellation, but cells with no station are thrown away, so a street we failed to match punches its
whole Voronoi cell out of the station that surrounds it. Both are clutter for the volunteer
editors, and land no other station covers belongs to the station around it.

So after the union + the existing despike close‑then‑open, stage05 **closes every interior ring
except the ones another station claims** — `close_unclaimed_holes(geom, keep_ring=…)`, with the
decision in `hole_is_claimed(...)`. Ring **area is not a reason to keep a hole**; ownership is.
A hole stays open when any of these hold:

1. **A foreign matched address is inside it** — filling would cover another station's address and
   break the invariant (checked with `_osm_foreign_overlap` over the coarse point grid, the same
   helper the OSM‑claim guard uses).
2. **Another station's polygon covers ≥ `POLYGON_HOLE_CLAIM_FRAC` (2 %) of it**, and more than
   `POLYGON_HOLE_CLAIM_M2` (50 m²) — address‑less coverage (whole‑settlement / OSM claims) is real
   coverage too, and filling would overlap it. The absolute floor stops despike fuzz along a shared
   border from reading as a claim.
3. **≥ `POLYGON_HOLE_FOREIGN_SETT_FRAC` (20 %) of it lies in a settlement this station has no
   coverage in.** A village whose addresses are *all* unmatched is invisible to checks 1 and 2, yet
   it is somebody else's coverage gap, not this station's land — e.g. Blaževo (7034300001, 11
   settlements, 74.7 km²) encloses the whole village of Боранци (38 addresses, none matched to any
   station) as an 11.8 km² "hole". That hole must stay. `station_setts` records the settlements each
   station has cells or claims in; the hole is tested against the naselje boundaries.

Checks 2 and 3 need every *other* station's polygon, so the main loop is **two passes**: pass 1
merges/clips/despikes each station into `merged_by_sid`, then the hole closing runs against an
STRtree over all of them. Under `--municipalities`, stations outside the scope aren't recomputed,
so their last‑published shapes are read back from `polygons.parquet` (`previous_polygons`) — without
that, a hole would be filled merely because its owner was out of scope.

Closing an unclaimed ring only adds land nobody else has, so it is overlap‑neutral by construction.

### Measured effect (full run, 7 885 stations, vs the territory‑fill baseline)

holes **3 341 → 1 844** (5 584 closed, 1 844 kept as another station's); vertices 1 050 503 →
**1 027 589**; parts 18 995 → 18 980; total area 60 376 → 60 485 km² (+109 km², the filled land);
**cross‑station address containments 27 738 → 27 738 — the exact same set, zero new ones**; own
addresses uncovered 216 → 215; 0 invalid geometries; largest station GeoJSON 41 436 → 40 944 B
(< 45 KB budget). Stage05 runtime ~2 min → ~3.5 min. Worked example: **Subotica #16**
(8043800016) held a 25 478 m² hole with *no* addresses of any kind — kept only because it was over
the old 5 000 m² threshold — now closed, while its 1 363 m² hole around station #17's address stays.

Filling case 1‑ and 2‑free holes deliberately absorbs **12 046 unmatched addresses** into the
polygons surrounding them (they belong to no station today). That is the accepted trade: cleaner
shapes for hand‑editing, and matching gaps are tracked in the review queue, not as map holes.

Note that §12.4's matched‑only seeding removes most holes *at the source* (an unmatched address no
longer punches out a cell), so this pass now mostly handles genuine tiling gaps. It stays as the
safety net — and after the residual fill it also has more work of a different kind: a station whose
filled territory wraps around a neighbour turns that neighbour into a true enclave, which is
correctly **kept** (check 2).

## 12.4 Filling the unclaimed land

Even with matched‑only seeds, land is left over. Settlements are tessellated **independently** and
each is clipped to its own naselje boundary, so whatever lies inside naselje A but nearer an
address in naselje B is dropped by both — A never claims it, B's cell is clipped away. Measured in
Subotica: **267 km² of the municipality's 1 007 km² belonged to nobody**, and 100 % of it sat inside
settlements that *do* have matched addresses. On the map that is the empty corridors cutting a
village station into islands, and the bays along the municipality edge.

Stage05 therefore runs a **residual fill** (pass 1.5, after the per‑station merge/clip/despike):

1. Per municipality, `residual = boundary − every station polygon`.
2. **Excluded:** settlements with no matched address at all. A village nobody was matched to is
   somebody else's coverage gap, not the neighbours' land — the same rule §12.3 check 3 applies to
   Боранци — so it stays visibly uncovered.
3. `residual_shares(residual, xy, sids)` splits what's left by **nearest matched address** — a
   Voronoi over the municipality's matched addresses. Each share is a subset of the residual, so it
   cannot touch an existing polygon, and each Voronoi cell holds exactly one address, so a share
   can never contain another station's address. The **invariant holds by the same argument as the
   territory fill itself**.
4. `attach_owner(piece, …)` routes every piece to a polygon it actually **merges** with: to its
   Voronoi owner when the piece borders that station, else to the adjoining station with the
   longest shared border, and **nowhere** if it borders nothing. Skipping this step cost real
   quality — pieces landing away from their owner became *new* islands (Subotica: 329 → 293 parts
   with naive filling, vs **329 → 246** with attachment). A piece holding a matched address always
   stays with its Voronoi owner, even detached: that address is by construction the owner's.

Pieces below `POLYGON_MIN_FILL_PIECE_M2` (25 m²) are skipped — naselje borders are lined with
sub‑metre slivers that are invisible on the map and cost more to route than they are worth.

### Measured effect (full run, 7 885 stations, vs the hole‑closing build)

parts **18 980 → 14 248** (−25 %); vertices 1 027 589 → 1 014 593; own addresses uncovered
**215 → 91**; total blob payload 46.8 → 44.7 MB; largest station GeoJSON 41 436 → **37 615 B**;
0 invalid. Foreign matched addresses covered **27 738 → 27 647** — 109 removed, 18 added, and 17 of
those 18 sit within 3.4 m of a shared border (the reprojection round‑trip limit; the 18th is
co‑located with one of the covering station's own addresses, so no geometry can separate them).
Holes 1 844 → 2 189: filling *creates* enclaves, because a station whose territory now wraps
around a neighbour turns that neighbour into a hole — all of them correctly kept as claimed.

Municipality coverage is the headline: **Subotica's unclaimed land went 267.5 → 0.3 km² of
1 007 km²**, its parts 329 → 246. Worked example — **Subotica #98** (Доњи Таванкут), the station
that prompted this: **13 parts → 2**, 9.19 → 13.18 km², 326 → 237 vertices. Боранци is untouched
(Blaževo #1 keeps its 11.8 km² hole), which is the excluded‑settlement rule doing its job.

### Performance — the fill must be done per station region, not per cell

The obvious implementation (intersect every Voronoi cell with the residual) made stage05 take
**33+ minutes**, with single municipalities at 460 s. Since `(A ∪ B) ∩ R == (A ∩ R) ∪ (B ∩ R)`,
unioning each station's cells *before* clipping to the residual gives an identical result while
turning tens of thousands of cell×residual intersections into a few dozen region×residual ones:
that municipality went **460 s → 27.6 s**, the full run to **9.2 min** (from 3.5 min before this
feature). The per‑municipality progress line exists because this pass is where the time goes.

## 12.5 Output & simplification

The polygon goes through the existing **adaptive simplify** loop — escalate the tolerance
`(5, 15, 40, 100, 250)` m until the GeoJSON fits the ~45 KB budget — then is written to
`polygons.parquet` and, in stage06, to the per‑municipality R2 blobs `polygons/m/<muni>.json`.
The reprojected geometry is validity‑repaired (`.buffer(0)` when `is_valid` is false — a
topology‑only op) so the output stays 100 % OGC‑valid. This is a **stage05‑only** change: no
schema, D1, or Worker‑logic change; the shapes just change.

**Simplification drift.** Simplifying moves the boundary by up to `tol`, and once the polygons
**tile** their municipality they share long borders — so a neighbour's address lying within that
band of a shared border can drift *inside*. This is not hypothetical: at 5 m it swallowed 37
addresses nationwide, every one of them within 3.4 m of the border. `drifted_over_foreign` catches
it by testing only the land the simplified shape *gained* (a thin band, so the check is cheap
enough to run for every station); when it fires, the tolerance backs off to 2 → 0.5 → 0 m. The size
budget stays the hard constraint: a station that cannot be both clean and small keeps the small
version. Measured: 79 stations drifted, **78 fixed** by a finer tolerance.

## 12.6 Reviewing before/after in the map

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
