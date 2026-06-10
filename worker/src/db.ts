/** D1 query helpers. All queries are scoped (by municipality / station / street set)
 *  to stay well under D1's response limits — never an unscoped scan of `addresses`. */

// An interval is [lo, hi] or [lo, hi, parity] where parity ∈ "all" | "odd" | "even".
// Serbian streets number odd/even on opposite sides, so a range may cover only one side.
export type Interval = [number, number] | [number, number, string];

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

export function houseInInterval(num: number, iv: Interval): boolean {
  const [lo, hi] = iv;
  if (num < lo || num > hi) return false;
  const p = intervalParity(iv);
  return p === "all" || (p === "odd" && num % 2 === 1) || (p === "even" && num % 2 === 0);
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
  manual_json: string | null;
  manual_locked: number;
  confidence: number;
  needs_review: number;
  parse_dialect: string | null;
  source: string;
  amendment_note: string | null;
  review_reason: string | null;
}

export function effectiveParsed(seg: SegmentRow): ParsedCoverage {
  const raw = seg.manual_json ?? seg.parsed_json;
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
              COALESCE(SUM(CASE WHEN cs.needs_review = 1 THEN 1 ELSE 0 END), 0) AS review_count
         FROM municipalities m
         LEFT JOIN polling_stations ps ON ps.municipality_id = m.id
         LEFT JOIN coverage_segments cs ON cs.station_id = ps.id
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
              COUNT(cs.id) AS seg_count,
              COALESCE(SUM(cs.needs_review), 0) AS review_count,
              CASE WHEN p.station_id IS NULL THEN 0 ELSE 1 END AS has_polygon,
              COALESCE(st.reviewed, 0) AS reviewed, COALESCE(st.dirty, 0) AS dirty
         FROM polling_stations ps
         LEFT JOIN coverage_segments cs ON cs.station_id = ps.id
         LEFT JOIN polygons p ON p.station_id = ps.id
         LEFT JOIN station_status st ON st.station_id = ps.id
        WHERE ps.municipality_id = ?
        GROUP BY ps.id
        ORDER BY review_count DESC, ps.number`
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
      `SELECT cs.*, s.name_cyr AS street_name_cyr, s.name_lat AS street_name_lat
         FROM coverage_segments cs
         LEFT JOIN streets s ON s.id = cs.street_id
        WHERE cs.station_id = ?
        ORDER BY cs.id`
    )
    .bind(stationId)
    .all<SegmentRow>();
  return results ?? [];
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
  const streetIds = [...new Set(segs.map((s) => s.street_id).filter((x): x is string => !!x))];
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
    if (!seg.street_id) continue;
    const parsed = effectiveParsed(seg);
    const singles = new Set(parsed.singles.map(([n, s]) => `${n}|${s}`));
    for (const a of byStreet.get(seg.street_id) ?? []) {
      if (a.house_num === null || seen.has(a.id)) continue;
      const inRange = parsed.intervals.some((iv) => houseInInterval(a.house_num!, iv));
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
