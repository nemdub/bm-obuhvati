/** D1 query helpers. All queries are scoped (by municipality / station / street set)
 *  to stay well under D1's response limits — never an unscoped scan of `addresses`. */

// An interval is [lo, hi], [lo, hi, parity], or [lo, hi, parity, loSfx, hiSfx].
// parity ∈ "all" | "odd" | "even" (Serbian streets number odd/even on opposite sides);
// loSfx/hiSfx are house-suffix bounds ("1-23ц" ends at 23ц: 23 and 23д included — azbuka
// order puts д before ц — while 23ш would be excluded).
export type Interval =
  | [number, number]
  | [number, number, string]
  | [number, number, string, string, string];

const SUFFIX_AZBUKA = "АБВГДЂЕЖЗИЈКЛЉМНЊОПРСТЋУФХЦЧЏШ";
function suffixRank(s: string): number[] {
  return [...s].map((ch) => {
    const i = SUFFIX_AZBUKA.indexOf(ch);
    return i >= 0 ? i : 100 + ch.charCodeAt(0);
  });
}
function rankCmp(a: number[], b: number[]): number {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const x = a[i] ?? -1, y = b[i] ?? -1;
    if (x !== y) return x - y;
  }
  return 0;
}

export interface ParsedCoverage {
  intervals: Interval[];
  singles: [number, string][];
  whole: boolean;
  // "бб" (bez broja): also cover the street's no-number (house_num IS NULL) houses.
  bez_broja: boolean;
  unknown_tokens?: string[];
}

export function intervalParity(iv: Interval): string {
  if (iv.length > 2 && iv[2]) return iv[2] as string;
  const [lo, hi] = iv;
  if (lo % 2 === 1 && hi % 2 === 1) return "odd";
  if (lo % 2 === 0 && hi % 2 === 0) return "even";
  return "all";
}

export function houseInInterval(num: number, suffix: string, iv: Interval): boolean {
  const [lo, hi] = iv;
  if (num < lo || num > hi) return false;
  const p = intervalParity(iv);
  if (!(p === "all" || (p === "odd" && num % 2 === 1) || (p === "even" && num % 2 === 0))) return false;
  const loSfx = (iv.length > 3 && iv[3]) || "";
  const hiSfx = (iv.length > 4 && iv[4]) || "";
  if (num === lo && loSfx && rankCmp(suffixRank(suffix), suffixRank(loSfx)) < 0) return false;
  if (num === hi && hiSfx && rankCmp(suffixRank(suffix), suffixRank(hiSfx)) > 0) return false;
  return true;
}

export interface MunicipalityRow {
  id: string;
  name_cyr: string;
  name_lat: string;
  station_count: number;
  review_count: number;
}

export interface StationRow {
  id: number;
  municipality_id: string;
  number: number;
  name_cyr: string;
  name_lat: string;
  address_cyr: string;
  address_lat: string;
  raw_coverage_text: string;
  is_amendment: number;
  /** Reviewer-added station (row in added_stations; id = ADDED_STATION_BASE + added id). */
  is_added?: number;
  /** Tombstoned via removed_stations (hidden from maps/exports; restorable). */
  removed?: number;
  /** Raw text comes from a station_text_overrides correction, not stage02 extraction. */
  text_overridden?: number;
  /** Settlements the matcher assumes this station covers (home + spanned). Read-only display. */
  assumed_settlements?: AssumedSettlement[];
}

export interface AssumedSettlement {
  id: string;
  role: string; // 'home' | 'spanned'
  name_cyr: string;
  name_lat: string;
}

export interface SegmentRow {
  id: number;
  station_id: number;
  settlement_raw: string | null;
  street_raw: string;
  street_id: string | null;
  street_name_cyr: string | null;
  street_name_lat: string | null;
  kind: string;
  parsed_json: string;
  /** Human override values (from segment_overrides; survive derived re-imports). */
  ov_json: string | null;
  ov_street_id: string | null;
  ov_reviewed: number | null;
  ov_street_name_cyr: string | null;
  ov_street_name_lat: string | null;
  confidence: number;
  needs_review: number;
  parse_dialect: string | null;
  source: string;
  amendment_note: string | null;
  review_reason: string | null;
  /** Set for reviewer-added street claims (row id in station_added_segments). */
  added_id?: number;
}

/** Synthetic segment-id base for reviewer-added claims (shared with the pipeline). */
export const ADDED_SEG_BASE = 9_000_000_000_000;

/** Synthetic station-id base for reviewer-added stations (shared with the pipeline).
 *  station id = ADDED_STATION_BASE + added_stations.id. Above ADDED_SEG_BASE's space and
 *  far above real ids (municipality_id * 100000 + n), so the three id spaces never collide. */
export const ADDED_STATION_BASE = 9_500_000_000_000;
export function isAddedStationId(id: number): boolean {
  return id >= ADDED_STATION_BASE;
}

/** Tagged street-id for a settlement (whole area) picked in the street pickers.
 *  Stored in segment_overrides.manual_street_id / station_added_segments.street_id
 *  (no FK there); the pipeline expands it to every street of the settlement. */
export const SETT_PICK_PREFIX = "sett:";
export function settIdOfPick(id: string | null | undefined): string | null {
  return id?.startsWith(SETT_PICK_PREFIX) ? id.slice(SETT_PICK_PREFIX.length) : null;
}

/** Sentinel stored in segment_overrides.manual_street_id when the reviewer confirms the
 *  street does not exist in the register: the segment is resolved (out of the review
 *  queue) but maps to no street, so no addresses/polygon are built for it. */
export const NONE_PICK = "none";

export function effectiveParsed(seg: SegmentRow): ParsedCoverage {
  const raw = seg.ov_json ?? seg.parsed_json;
  try {
    const p = JSON.parse(raw) as Partial<ParsedCoverage>;
    return {
      intervals: p.intervals ?? [], singles: p.singles ?? [],
      whole: !!p.whole, bez_broja: !!p.bez_broja,
    };
  } catch {
    return { intervals: [], singles: [], whole: false, bez_broja: false };
  }
}

export async function listMunicipalities(db: D1Database): Promise<MunicipalityRow[]> {
  const { results } = await db
    .prepare(
      `SELECT m.id, m.name_cyr, m.name_lat,
              COUNT(DISTINCT ps.id) AS station_count,
              COALESCE(SUM(CASE WHEN cs.needs_review = 1 AND COALESCE(o.reviewed, 0) = 0
                                THEN 1 ELSE 0 END), 0) AS review_count
         FROM municipalities m
         LEFT JOIN polling_stations ps ON ps.municipality_id = m.id
         LEFT JOIN coverage_segments cs ON cs.station_id = ps.id
         LEFT JOIN segment_overrides o ON o.segment_id = cs.id
        WHERE m.parent_id IS NULL
        GROUP BY m.id
        ORDER BY m.name_lat`
    )
    .all<MunicipalityRow>();
  return results ?? [];
}

export async function getMunicipality(db: D1Database, id: string) {
  return db.prepare("SELECT id, name_cyr, name_lat FROM municipalities WHERE id = ?").bind(id)
    .first<{ id: string; name_cyr: string; name_lat: string }>();
}

export async function listStations(db: D1Database, muniId: string) {
  const { results } = await db
    .prepare(
      `SELECT ps.id, ps.number, ps.section_cyr, ps.name_cyr, ps.name_lat, ps.address_cyr, ps.address_lat, ps.is_amendment,
              COUNT(DISTINCT cs.id) AS seg_count,
              COALESCE(SUM(CASE WHEN cs.needs_review = 1 AND COALESCE(o.reviewed, 0) = 0
                                THEN 1 ELSE 0 END), 0) AS review_count,
              COALESCE(st.reviewed, 0) AS reviewed, COALESCE(st.dirty, 0) AS dirty,
              CASE WHEN rm.station_id IS NOT NULL THEN 1 ELSE 0 END AS removed,
              0 AS is_added
         FROM polling_stations ps
         LEFT JOIN coverage_segments cs ON cs.station_id = ps.id
         LEFT JOIN segment_overrides o ON o.segment_id = cs.id
         LEFT JOIN station_status st ON st.station_id = ps.id
         LEFT JOIN removed_stations rm ON rm.station_id = ps.id
        WHERE ps.municipality_id = ?
        GROUP BY ps.id
        -- id is the running index → keeps a member town's sub-table (Kostolac/Sevojno)
        -- contiguous after the city block, so section dividers render correctly.
        ORDER BY ps.id`
    )
    .bind(muniId)
    .all<{ id: number; number: number } & Record<string, unknown>>();
  const rows = results ?? [];

  // Reviewer-added stations (worker-owned). After a recompute they also exist as real
  // polling_stations rows (id = ADDED_STATION_BASE + added.id) — skip those to avoid
  // duplicates; additions made since the last recompute still show live with no segments yet.
  const existing = new Set(rows.map((r) => r.id));
  const { results: added } = await db
    .prepare(
      `SELECT a.id, a.number, a.name_cyr, a.address_cyr,
              COALESCE(st.dirty, 0) AS dirty
         FROM added_stations a
         LEFT JOIN station_status st ON st.station_id = ? + a.id
        WHERE a.municipality_id = ? ORDER BY a.id`
    )
    .bind(ADDED_STATION_BASE, muniId)
    .all<{ id: number; number: number | null; name_cyr: string; address_cyr: string | null; dirty: number }>();
  for (const a of added ?? []) {
    const synthId = ADDED_STATION_BASE + a.id;
    if (existing.has(synthId)) continue;
    rows.push({
      id: synthId, number: a.number ?? 0, section_cyr: null, name_cyr: a.name_cyr, name_lat: a.name_cyr,
      address_cyr: a.address_cyr ?? "", address_lat: a.address_cyr ?? "", is_amendment: 0,
      seg_count: 0, review_count: 0, reviewed: 0, dirty: a.dirty, removed: 0, is_added: 1,
    } as (typeof rows)[number]);
  }
  rows.sort((a, b) => Number(a.number) - Number(b.number));
  return rows;
}

export async function getStation(db: D1Database, id: number): Promise<StationRow | null> {
  if (isAddedStationId(id)) {
    const a = await db
      .prepare("SELECT * FROM added_stations WHERE id = ?")
      .bind(id - ADDED_STATION_BASE)
      .first<{ id: number; municipality_id: string; number: number | null;
               name_cyr: string; address_cyr: string | null; raw_coverage_text: string }>();
    if (a) {
      return {
        id, municipality_id: a.municipality_id, number: a.number ?? 0,
        name_cyr: a.name_cyr, name_lat: a.name_cyr,
        address_cyr: a.address_cyr ?? "", address_lat: a.address_cyr ?? "",
        raw_coverage_text: a.raw_coverage_text, is_amendment: 0, is_added: 1,
      };
    }
    // Fall through: a recomputed added station is a real polling_stations row.
  }
  const st = await db
    .prepare(
      `SELECT ps.*, t.raw_coverage_text AS ov_text,
              CASE WHEN rm.station_id IS NOT NULL THEN 1 ELSE 0 END AS removed
         FROM polling_stations ps
         LEFT JOIN station_text_overrides t ON t.station_id = ps.id
         LEFT JOIN removed_stations rm ON rm.station_id = ps.id
        WHERE ps.id = ?`
    )
    .bind(id)
    .first<StationRow & { ov_text: string | null; removed: number }>();
  if (!st) return null;
  if (st.ov_text != null) {
    st.raw_coverage_text = st.ov_text;
    st.text_overridden = 1;
  }
  st.assumed_settlements = await assumedSettlements(db, id);
  return st;
}

/** Settlements the matcher assumes a station covers (home first, then spanned alphabetically). */
export async function assumedSettlements(db: D1Database, stationId: number): Promise<AssumedSettlement[]> {
  const { results } = await db
    .prepare(
      `SELECT ss.settlement_id AS id, ss.role, s.name_cyr, s.name_lat
         FROM station_settlements ss
         JOIN settlements s ON s.id = ss.settlement_id
        WHERE ss.station_id = ?
        ORDER BY (ss.role = 'home') DESC, s.name_cyr`
    )
    .bind(stationId)
    .all<AssumedSettlement>();
  return results ?? [];
}

/** Ids of stations tombstoned in a municipality — excluded from maps and exports so a
 *  removed station disappears immediately, before its R2 polygon blob is rebuilt. */
export async function removedStationIds(db: D1Database, muniId: string): Promise<Set<number>> {
  const { results } = await db
    .prepare(
      `SELECT rm.station_id FROM removed_stations rm
         JOIN polling_stations ps ON ps.id = rm.station_id
        WHERE ps.municipality_id = ?`
    )
    .bind(muniId)
    .all<{ station_id: number }>();
  return new Set((results ?? []).map((r) => r.station_id));
}

export async function getSegments(db: D1Database, stationId: number): Promise<SegmentRow[]> {
  const { results } = await db
    .prepare(
      `SELECT cs.*, s.name_cyr AS street_name_cyr, s.name_lat AS street_name_lat,
              o.manual_json AS ov_json, o.manual_street_id AS ov_street_id,
              o.reviewed AS ov_reviewed,
              COALESCE(ms.name_cyr, mst.name_cyr) AS ov_street_name_cyr,
              COALESCE(ms.name_lat, mst.name_lat) AS ov_street_name_lat
         FROM coverage_segments cs
         LEFT JOIN streets s ON s.id = cs.street_id
         LEFT JOIN segment_overrides o ON o.segment_id = cs.id
         LEFT JOIN streets ms ON ms.id = o.manual_street_id
         LEFT JOIN settlements mst ON 'sett:' || mst.id = o.manual_street_id
        WHERE cs.station_id = ?
        ORDER BY cs.id`
    )
    .bind(stationId)
    .all<SegmentRow>();
  const segs = results ?? [];

  // Reviewer-added street claims. After a pipeline recompute these exist in
  // coverage_segments too (as id = ADDED_SEG_BASE + added.id) — skip those to avoid
  // duplicates; additions made since the last recompute still show live.
  const { results: added } = await db
    .prepare(
      `SELECT a.id AS aid, a.station_id, a.street_id, a.manual_json,
              COALESCE(s.name_cyr, st.name_cyr) AS name_cyr,
              COALESCE(s.name_lat, st.name_lat) AS name_lat
         FROM station_added_segments a
         LEFT JOIN streets s ON s.id = a.street_id
         LEFT JOIN settlements st ON 'sett:' || st.id = a.street_id
        WHERE a.station_id = ? ORDER BY a.id`
    )
    .bind(stationId)
    .all<{ aid: number; station_id: number; street_id: string; manual_json: string; name_cyr: string; name_lat: string }>();
  const existing = new Set(segs.map((s) => s.id));
  for (const a of added ?? []) {
    const synthId = ADDED_SEG_BASE + a.aid;
    if (existing.has(synthId)) {
      const row = segs.find((s) => s.id === synthId)!;
      row.added_id = a.aid;
      continue;
    }
    segs.push({
      id: synthId, station_id: a.station_id, settlement_raw: null,
      street_raw: a.name_cyr, street_id: a.street_id,
      street_name_cyr: a.name_cyr, street_name_lat: a.name_lat,
      kind: "manual_added", parsed_json: a.manual_json,
      ov_json: null, ov_street_id: null, ov_reviewed: null,
      ov_street_name_cyr: null, ov_street_name_lat: null,
      confidence: 0.9, needs_review: 0, parse_dialect: "manual",
      source: "added", amendment_note: null, review_reason: null,
      added_id: a.aid,
    } as SegmentRow);
  }
  return segs;
}

/** One station's stored polygon plus the metadata the maps and exports need. Mirrors the
 *  old `polygons ⋈ polling_stations` D1 row exactly, so every consumer is unchanged. */
export interface PolyRow {
  station_id: number;
  number: number;
  name_cyr: string;
  name_lat: string;
  address_cyr: string;
  address_lat: string;
  geojson: string; // GeoJSON MultiPolygon (occasionally Polygon), as a string
  area_m2: number;
  point_count: number;
  computed_at: string;
  // Per-segment OSM-fallback claim geometry, as a JSON string `[{segment_id, geojson}]` (or
  // null). These shapes have no addresses, so the review UI uses them to draw/zoom an OSM match.
  osm?: string | null;
}

/** Read a municipality's polygon blob from R2 (polygons/m/<muniId>.json). Polygons are
 *  static between recomputes and byte-heavy, so they live in object storage, not D1.
 *  Returns the stations array (pipeline-sorted by number) or [] if the blob is absent. */
export async function muniPolyRows(bucket: R2Bucket, muniId: string): Promise<PolyRow[]> {
  const obj = await bucket.get(`polygons/m/${muniId}.json`);
  if (!obj) return [];
  const data = await obj.json<{ stations: PolyRow[] }>();
  return data.stations ?? [];
}

interface AddrRow {
  id: number;
  street_id: string;
  house_num: number | null;
  house_suffix: string;
  house_raw: string;
  lat: number;
  lon: number;
}

/** Live-compute matched address points for a station from its EFFECTIVE segments
 *  (manual override || parsed), so edits are reflected immediately on the map. */
// Streets each segment spans. Settlement claims cover EVERY street of the settlement:
// pipeline-derived ones ("Белосавци" = the whole village) anchor on one street and are
// marked via review_reason; reviewer picks carry a tagged "sett:<id>" directly.
async function resolveSegStreets(db: D1Database, segs: SegmentRow[]) {
  const effStreet = (s: SegmentRow) => s.ov_street_id ?? s.street_id;
  const segStreets = new Map<number, string[]>(); // seg id -> register street ids
  for (const s of segs) {
    const eff = effStreet(s);
    if (!eff || eff === NONE_PICK) continue; // "doesn't exist" — no addresses to map
    const pickedSett = settIdOfPick(eff);
    if (pickedSett) {
      const { results: ext } = await db.prepare(
        `SELECT id FROM streets WHERE settlement_id = ?`
      ).bind(pickedSett).all<{ id: string }>();
      segStreets.set(s.id, (ext ?? []).map((r) => r.id));
    } else if ((s.review_reason ?? "").includes("settlement_claim") && !s.ov_street_id) {
      const { results: ext } = await db.prepare(
        `SELECT id FROM streets WHERE settlement_id = (SELECT settlement_id FROM streets WHERE id = ?)`
      ).bind(eff).all<{ id: string }>();
      segStreets.set(s.id, (ext ?? []).map((r) => r.id));
    } else {
      segStreets.set(s.id, [eff]);
    }
  }
  return segStreets;
}

// D1 caps bound parameters at 100 per query. A whole-settlement claim can span hundreds of
// streets (e.g. a leading town heading expands to every street of the town — 534 for
// Пожаревац, 1498 for Крагујевац), so run the `street_id IN (...)` lookups in chunks under
// the cap and concatenate, rather than binding all ids in one statement (which throws → 500).
const D1_IN_CHUNK = 90;

async function selectByStreetIds<T>(
  db: D1Database,
  sqlFor: (placeholders: string) => string,
  streetIds: string[]
): Promise<T[]> {
  const out: T[] = [];
  for (let i = 0; i < streetIds.length; i += D1_IN_CHUNK) {
    const chunk = streetIds.slice(i, i + D1_IN_CHUNK);
    const placeholders = chunk.map(() => "?").join(",");
    const { results } = await db.prepare(sqlFor(placeholders)).bind(...chunk).all<T>();
    if (results) out.push(...results);
  }
  return out;
}

export async function pointsForStation(db: D1Database, stationId: number) {
  const segs = await getSegments(db, stationId);
  const segStreets = await resolveSegStreets(db, segs);
  const streetIds = [...new Set([...segStreets.values()].flat())];
  if (streetIds.length === 0) return { type: "FeatureCollection", features: [] };

  const results = await selectByStreetIds<AddrRow>(
    db,
    (ph) => `SELECT id, street_id, house_num, house_suffix, house_raw, lat, lon
         FROM addresses WHERE street_id IN (${ph})`,
    streetIds
  );

  const byStreet = new Map<string, AddrRow[]>();
  for (const a of results) {
    const arr = byStreet.get(a.street_id) ?? [];
    arr.push(a);
    byStreet.set(a.street_id, arr);
  }

  const seen = new Set<number>();
  const features: unknown[] = [];
  for (const seg of segs) {
    const ids = segStreets.get(seg.id);
    if (!ids) continue;
    const parsed = effectiveParsed(seg);
    const singles = new Set(parsed.singles.map(([n, s]) => `${n}|${s}`));
    const segAddrs = ids.flatMap((x) => byStreet.get(x) ?? []);
    for (const a of segAddrs) {
      if (seen.has(a.id)) continue;
      // No-number houses (house_num IS NULL): covered by a whole-street or "бб" claim only.
      if (a.house_num === null) {
        if (parsed.whole || parsed.bez_broja) {
          seen.add(a.id);
          features.push({
            type: "Feature",
            geometry: { type: "Point", coordinates: [a.lon, a.lat] },
            properties: {
              segment_id: seg.id,
              house: a.house_raw,
              confidence: seg.confidence,
              needs_review: seg.needs_review,
            },
          });
        }
        continue;
      }
      const inRange = parsed.intervals.some((iv) => houseInInterval(a.house_num!, a.house_suffix, iv));
      // Exact (num+suffix) match, or a bare number implying its suffixed variants
      // (5 -> 5а/5б/...). Cross-station "unless listed elsewhere" overrides are resolved
      // in the pipeline; this live preview approximates by matching the bare number.
      const isSingle =
        singles.has(`${a.house_num}|${a.house_suffix}`) || singles.has(`${a.house_num}|`);
      if (parsed.whole || inRange || isSingle) {
        seen.add(a.id);
        features.push({
          type: "Feature",
          geometry: { type: "Point", coordinates: [a.lon, a.lat] },
          properties: {
            segment_id: seg.id,
            house: a.house_raw,
            confidence: seg.confidence,
            needs_review: seg.needs_review,
          },
        });
      }
    }
  }
  return { type: "FeatureCollection", features };
}

// Stored line geometry for a station's segments whose street has no addresses (so it
// produces no points) — lets the UI show WHERE such a street is even though it can't
// be covered by address points. Each feature carries its segment_id for highlighting.
export async function streetLinesForStation(db: D1Database, stationId: number) {
  const segs = await getSegments(db, stationId);
  const segStreets = await resolveSegStreets(db, segs);
  const streetIds = [...new Set([...segStreets.values()].flat())];
  if (streetIds.length === 0) return { type: "FeatureCollection", features: [] };

  const results = await selectByStreetIds<{ street_id: string; geojson: string }>(
    db,
    (ph) => `SELECT street_id, geojson FROM street_geometry WHERE street_id IN (${ph})`,
    streetIds
  );
  const byStreet = new Map(results.map((r) => [r.street_id, r.geojson]));

  const features: unknown[] = [];
  for (const seg of segs) {
    const ids = segStreets.get(seg.id);
    if (!ids) continue;
    for (const sid of ids) {
      const gj = byStreet.get(sid);
      if (gj) features.push({ type: "Feature", geometry: JSON.parse(gj), properties: { segment_id: seg.id } });
    }
  }
  return { type: "FeatureCollection", features };
}

/** All of a municipality's station polygons (for the overview map and exports). */
export async function allMuniPolygons(bucket: R2Bucket, muniId: string): Promise<PolyRow[]> {
  return muniPolyRows(bucket, muniId);
}

/** Search register streets AND settlements within a station's municipality, for the
 *  manual pickers — a "street" in the document is sometimes a whole village or city
 *  area. Settlement rows come first, with the tagged "sett:<id>" pick id and area = 1.
 *  Matches the normalized Cyrillic key and (ASCII-case-insensitively) the Latin name. */
export async function searchStreets(db: D1Database, stationId: number, q: string) {
  const st = await db.prepare("SELECT municipality_id FROM polling_stations WHERE id = ?")
    .bind(stationId).first<{ municipality_id: string }>();
  if (!st) return [];
  const needle = `%${q.toUpperCase()}%`;
  type Hit = { id: string; name_cyr: string; name_lat: string;
               settlement_cyr: string | null; settlement_lat: string | null; area: number };
  const [setts, streets] = await Promise.all([
    db.prepare(
      `SELECT 'sett:' || id AS id, name_cyr, name_lat,
              NULL AS settlement_cyr, NULL AS settlement_lat, 1 AS area
         FROM settlements
        WHERE municipality_id = ?1
          AND (UPPER(name_cyr) LIKE ?2 OR UPPER(name_lat) LIKE ?2)
        ORDER BY name_cyr LIMIT 10`
    ).bind(st.municipality_id, needle).all<Hit>(),
    db.prepare(
      `SELECT s.id, s.name_cyr, s.name_lat,
              st.name_cyr AS settlement_cyr, st.name_lat AS settlement_lat, 0 AS area
         FROM streets s JOIN settlements st ON st.id = s.settlement_id
        WHERE st.municipality_id = ?1
          AND (s.name_norm LIKE ?2 OR UPPER(s.name_lat) LIKE ?2)
        ORDER BY st.name_cyr, s.name_cyr LIMIT 30`
    ).bind(st.municipality_id, needle).all<Hit>(),
  ]);
  return [...(setts.results ?? []), ...(streets.results ?? [])];
}

/** All boundary outlines for the homepage overview map. Grouped members (Užice+Sevojno
 *  etc.) report their representative's id, whose page/stats own their stations. */
export async function allBoundaries(db: D1Database) {
  const { results } = await db
    .prepare(
      `SELECT b.geojson, COALESCE(m.parent_id, m.id) AS municipality_id
         FROM muni_boundaries b JOIN municipalities m ON m.id = b.municipality_id`
    )
    .all<{ geojson: string; municipality_id: string }>();
  return results ?? [];
}

/** Dataset-wide polygon totals for the homepage summary cards. Precomputed by the pipeline
 *  into polygons/summary.json (a full scan of R2 blobs would be wasteful on the hot path). */
export async function summaryStats(bucket: R2Bucket) {
  const obj = await bucket.get("polygons/summary.json");
  if (!obj) return { polygon_count: 0, matched_addresses: 0 };
  return obj.json<{ polygon_count: number; matched_addresses: number }>();
}

/** Official boundary outline(s) for a municipality — includes grouped members' outlines
 *  (Užice+Sevojno etc.), whose stations share the page and span both territories. */
export async function muniBoundaries(db: D1Database, muniId: string) {
  const { results } = await db
    .prepare(
      `SELECT geojson FROM muni_boundaries
        WHERE municipality_id = ?
           OR municipality_id IN (SELECT id FROM municipalities WHERE parent_id = ?)`
    )
    .bind(muniId, muniId)
    .all<{ geojson: string }>();
  return results ?? [];
}

