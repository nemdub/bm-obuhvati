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

export function effectiveParsed(seg: SegmentRow): ParsedCoverage {
  const raw = seg.ov_json ?? seg.parsed_json;
  try {
    const p = JSON.parse(raw) as Partial<ParsedCoverage>;
    return { intervals: p.intervals ?? [], singles: p.singles ?? [], whole: !!p.whole };
  } catch {
    return { intervals: [], singles: [], whole: false };
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
      `SELECT ps.id, ps.number, ps.name_cyr, ps.name_lat, ps.is_amendment,
              COUNT(DISTINCT cs.id) AS seg_count,
              COALESCE(SUM(CASE WHEN cs.needs_review = 1 AND COALESCE(o.reviewed, 0) = 0
                                THEN 1 ELSE 0 END), 0) AS review_count,
              CASE WHEN p.station_id IS NULL THEN 0 ELSE 1 END AS has_polygon,
              COALESCE(st.reviewed, 0) AS reviewed, COALESCE(st.dirty, 0) AS dirty
         FROM polling_stations ps
         LEFT JOIN coverage_segments cs ON cs.station_id = ps.id
         LEFT JOIN segment_overrides o ON o.segment_id = cs.id
         LEFT JOIN polygons p ON p.station_id = ps.id
         LEFT JOIN station_status st ON st.station_id = ps.id
        WHERE ps.municipality_id = ?
        GROUP BY ps.id
        ORDER BY ps.number`
    )
    .bind(muniId)
    .all();
  return results ?? [];
}

export async function getStation(db: D1Database, id: number) {
  return db.prepare("SELECT * FROM polling_stations WHERE id = ?").bind(id).first<StationRow>();
}

export async function getSegments(db: D1Database, stationId: number): Promise<SegmentRow[]> {
  const { results } = await db
    .prepare(
      `SELECT cs.*, s.name_cyr AS street_name_cyr, s.name_lat AS street_name_lat,
              o.manual_json AS ov_json, o.manual_street_id AS ov_street_id,
              o.reviewed AS ov_reviewed,
              ms.name_cyr AS ov_street_name_cyr, ms.name_lat AS ov_street_name_lat
         FROM coverage_segments cs
         LEFT JOIN streets s ON s.id = cs.street_id
         LEFT JOIN segment_overrides o ON o.segment_id = cs.id
         LEFT JOIN streets ms ON ms.id = o.manual_street_id
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
              s.name_cyr, s.name_lat
         FROM station_added_segments a JOIN streets s ON s.id = a.street_id
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

export async function getPolygon(db: D1Database, stationId: number) {
  return db.prepare("SELECT geojson, area_m2, point_count, computed_at FROM polygons WHERE station_id = ?")
    .bind(stationId)
    .first<{ geojson: string; area_m2: number; point_count: number; computed_at: string }>();
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
export async function pointsForStation(db: D1Database, stationId: number) {
  const segs = await getSegments(db, stationId);
  const effStreet = (s: SegmentRow) => s.ov_street_id ?? s.street_id;
  const streetIds = [...new Set(segs.map(effStreet).filter((x): x is string => !!x))];
  if (streetIds.length === 0) return { type: "FeatureCollection", features: [] };

  const placeholders = streetIds.map(() => "?").join(",");
  const { results } = await db
    .prepare(
      `SELECT id, street_id, house_num, house_suffix, house_raw, lat, lon
         FROM addresses WHERE street_id IN (${placeholders})`
    )
    .bind(...streetIds)
    .all<AddrRow>();

  const byStreet = new Map<string, AddrRow[]>();
  for (const a of results ?? []) {
    const arr = byStreet.get(a.street_id) ?? [];
    arr.push(a);
    byStreet.set(a.street_id, arr);
  }

  const seen = new Set<number>();
  const features: unknown[] = [];
  for (const seg of segs) {
    const sid = effStreet(seg);
    if (!sid) continue;
    const parsed = effectiveParsed(seg);
    const singles = new Set(parsed.singles.map(([n, s]) => `${n}|${s}`));
    for (const a of byStreet.get(sid) ?? []) {
      if (a.house_num === null || seen.has(a.id)) continue;
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

export async function allMuniPolygons(db: D1Database, muniId: string) {
  const { results } = await db
    .prepare(
      `SELECT p.station_id, ps.number, ps.name_cyr, ps.name_lat, p.geojson
         FROM polygons p JOIN polling_stations ps ON ps.id = p.station_id
        WHERE ps.municipality_id = ?
        ORDER BY ps.number`
    )
    .bind(muniId)
    .all<{ station_id: number; number: number; name_cyr: string; name_lat: string; geojson: string }>();
  return results ?? [];
}

/** Search register streets within a station's municipality, for the manual street picker.
 *  Matches the normalized Cyrillic key and (ASCII-case-insensitively) the Latin name. */
export async function searchStreets(db: D1Database, stationId: number, q: string) {
  const st = await db.prepare("SELECT municipality_id FROM polling_stations WHERE id = ?")
    .bind(stationId).first<{ municipality_id: string }>();
  if (!st) return [];
  const needle = `%${q.toUpperCase()}%`;
  const { results } = await db
    .prepare(
      `SELECT s.id, s.name_cyr, s.name_lat, st.name_cyr AS settlement_cyr, st.name_lat AS settlement_lat
         FROM streets s JOIN settlements st ON st.id = s.settlement_id
        WHERE st.municipality_id = ?
          AND (s.name_norm LIKE ? OR UPPER(s.name_lat) LIKE ?)
        ORDER BY st.name_cyr, s.name_cyr LIMIT 30`
    )
    .bind(st.municipality_id, needle, needle)
    .all<{ id: string; name_cyr: string; name_lat: string; settlement_cyr: string; settlement_lat: string }>();
  return results ?? [];
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

/** Dataset-wide polygon totals for the homepage summary cards. */
export async function summaryStats(db: D1Database) {
  const row = await db
    .prepare(
      `SELECT COUNT(*) AS polygon_count, COALESCE(SUM(point_count), 0) AS matched_addresses
         FROM polygons`
    )
    .first<{ polygon_count: number; matched_addresses: number }>();
  return row ?? { polygon_count: 0, matched_addresses: 0 };
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

export async function muniPolygons(db: D1Database, muniId: string, excludeStation: number) {
  const { results } = await db
    .prepare(
      `SELECT p.station_id, p.geojson
         FROM polygons p JOIN polling_stations ps ON ps.id = p.station_id
        WHERE ps.municipality_id = ? AND p.station_id != ?`
    )
    .bind(muniId, excludeStation)
    .all<{ station_id: number; geojson: string }>();
  return results ?? [];
}
