import { Hono } from "hono";
import type { Env } from "./types";
import { tr } from "./translit";
import { REVIEW_REASONS } from "./i18n";
import {
  listMunicipalities, getMunicipality, listStations, getStation, getSegments,
  pointsForStation, streetLinesForStation, allMuniPolygons, effectiveParsed, searchStreets,
  muniBoundaries, allBoundaries, summaryStats, settIdOfPick, NONE_PICK,
  removedStationIds, ADDED_STATION_BASE, isAddedStationId,
} from "./db";
import { getScript, municipalitiesView, stationsView, stationDetailView } from "./views";
import { buildGeoJSON, buildKML, muniSlug, type ExportRow } from "./export";

const app = new Hono<{ Bindings: Env }>();

// Persist the script choice when toggled via ?script=; never edge-cache dynamic pages
// (review counts and edits must show immediately — static assets are served separately).
app.use("*", async (c, next) => {
  const q = c.req.query("script");
  if (q === "lat" || q === "cyr") {
    c.header("Set-Cookie", `script=${q}; Path=/; Max-Age=31536000; SameSite=Lax`);
  }
  c.header("Cache-Control", "no-store");
  await next();
});

// ── Pages ───────────────────────────────────────────────────────────────────
// Serbian abeceda ordering + Belgrade nesting are handled in the view.
app.get("/", async (c) => {
  const [munis, stats] = await Promise.all([listMunicipalities(c.env.DB), summaryStats(c.env.POLY)]);
  return c.html(municipalitiesView(c, munis, stats));
});

app.get("/m/:id", async (c) => {
  const muni = await getMunicipality(c.env.DB, c.req.param("id"));
  if (!muni) return c.notFound();
  return c.html(stationsView(c, muni, await listStations(c.env.DB, muni.id)));
});

app.get("/s/:id", async (c) => {
  const st = await getStation(c.env.DB, Number(c.req.param("id")));
  if (!st) return c.notFound();
  const muni = await getMunicipality(c.env.DB, st.municipality_id);
  return c.html(stationDetailView(c, st, muni?.name_cyr ?? st.municipality_id));
});

// ── API ─────────────────────────────────────────────────────────────────────
app.get("/api/s/:id/segments", async (c) => {
  const script = getScript(c);
  const segs = await getSegments(c.env.DB, Number(c.req.param("id")));
  return c.json(
    segs.map((s) => {
      // Village-name claims anchor street_id on an arbitrary street of the settlement
      // (stage04 picks streets[0]) — its register name must not be shown as the title.
      const settlementClaim =
        (s.review_reason ?? "").includes("settlement_claim") && !s.ov_street_id;
      // Reviewer confirmed the street does not exist in the register (sentinel pick).
      const streetMissing = s.ov_street_id === NONE_PICK;
      return {
        id: s.id,
        street_raw: tr(s.street_raw, script),
        street_name: s.ov_street_name_cyr
          ? tr(s.ov_street_name_cyr, script)
          : settlementClaim
            ? tr(s.street_raw, script)
            : s.street_name_cyr ? tr(s.street_name_cyr, script) : null,
        street_resolved: !streetMissing && !!(s.ov_street_id ?? s.street_id),
        street_missing: streetMissing,
        manual_street: !!s.ov_street_id && !streetMissing,
        manual_street_id: streetMissing ? null : s.ov_street_id,
        kind: s.kind,
        parsed: effectiveParsed(s),
        manual_locked: s.ov_json != null || s.ov_street_id != null ? 1 : 0,
        confidence: s.confidence,
        needs_review: s.needs_review && !s.ov_reviewed ? 1 : 0,
        review_reasons: (s.review_reason ?? "")
          .split(",")
          .filter(Boolean)
          .map((code) => {
            // Codes may carry a parameter after ':' (e.g. "conflict:7|12" = opposing station numbers).
            const [base, param] = code.split(":");
            let text = tr(REVIEW_REASONS[base] ?? base, script);
            // For name-based matches, spell out document spelling -> matched register name
            // (the card title shows the resolved name, so the discrepancy isn't otherwise visible).
            if (
              (base === "fuzzy" || base === "muni_fallback" || base === "alias" || base === "proximity" || base === "abbrev") &&
              s.street_id
            ) {
              const matched = (script === "lat" ? s.street_name_lat : s.street_name_cyr) ?? "";
              text += `: „${tr(s.street_raw, script)}“ → „${matched}“`;
            }
            if (base === "conflict" && param) {
              text += ` (${tr("бр.", script)} ${param.split("|").join(", ")})`;
            }
            if (base === "ambiguous" && param) {
              text += `: ${tr(param.split("|").join(", "), script)}`;
            }
            if (base === "settlement_claim" && param) {
              text += `: ${tr(param, script)}`;
            }
            return text;
          }),
        source: s.source,
        added_id: s.added_id ?? null,
        amendment_note: s.amendment_note ? tr(s.amendment_note, script) : null,
        };
    })
  );
});

app.put("/api/segments/:id", async (c) => {
  const segId = Number(c.req.param("id"));
  const body = await c.req.json<{
    intervals?: [number, number][]; singles?: [number, string][]; whole?: boolean;
    bez_broja?: boolean; reviewed?: boolean; street_id?: string | null;
  }>();
  const manual = JSON.stringify({
    intervals: body.intervals ?? [],
    singles: body.singles ?? [],
    whole: !!body.whole,
    bez_broja: !!body.bez_broja,
  });
  const seg = await c.env.DB.prepare("SELECT station_id FROM coverage_segments WHERE id = ?")
    .bind(segId).first<{ station_id: number }>();
  if (!seg) return c.notFound();
  // Human edits live in segment_overrides (survives derived re-imports).
  await c.env.DB.prepare(
    `INSERT INTO segment_overrides (segment_id, manual_json, manual_street_id, reviewed, updated_at)
     VALUES (?1, ?2, ?3, ?4, datetime('now'))
     ON CONFLICT(segment_id) DO UPDATE SET
       manual_json = ?2, manual_street_id = ?3,
       reviewed = MAX(segment_overrides.reviewed, ?4), updated_at = datetime('now')`
  ).bind(segId, manual, body.street_id ?? null, body.reviewed ? 1 : 0).run();
  await markDirty(c.env.DB, seg.station_id);
  return c.json({ ok: true });
});

app.delete("/api/segments/:id/manual", async (c) => {
  const segId = Number(c.req.param("id"));
  const seg = await c.env.DB.prepare("SELECT station_id FROM coverage_segments WHERE id = ?")
    .bind(segId).first<{ station_id: number }>();
  if (!seg) return c.notFound();
  await c.env.DB.prepare("DELETE FROM segment_overrides WHERE segment_id = ?").bind(segId).run();
  await markDirty(c.env.DB, seg.station_id);
  return c.json({ ok: true });
});

// Add a reviewer street claim (for streets the document omitted entirely).
app.post("/api/s/:id/segments", async (c) => {
  const stationId = Number(c.req.param("id"));
  const body = await c.req.json<{
    street_id: string; whole?: boolean; bez_broja?: boolean;
    intervals?: unknown[]; singles?: unknown[];
  }>();
  if (!body.street_id) return c.json({ ok: false, error: "street_id required" }, 400);
  const settId = settIdOfPick(body.street_id);
  const target = settId
    ? await c.env.DB.prepare("SELECT id FROM settlements WHERE id = ?").bind(settId).first()
    : await c.env.DB.prepare("SELECT id FROM streets WHERE id = ?").bind(body.street_id).first();
  if (!target) return c.json({ ok: false, error: "unknown street" }, 400);
  const manual = JSON.stringify({
    intervals: body.intervals ?? [],
    singles: body.singles ?? [],
    whole: body.whole ?? true,
    bez_broja: !!body.bez_broja,
  });
  await c.env.DB.prepare(
    `INSERT INTO station_added_segments (station_id, street_id, manual_json, created_at)
     VALUES (?, ?, ?, datetime('now'))`
  ).bind(stationId, body.street_id, manual).run();
  await markDirty(c.env.DB, stationId);
  return c.json({ ok: true });
});

// Edit an existing reviewer street claim (refine its coverage after adding).
app.put("/api/added/:addedId", async (c) => {
  const aid = Number(c.req.param("addedId"));
  const body = await c.req.json<{
    whole?: boolean; bez_broja?: boolean; intervals?: unknown[]; singles?: unknown[];
  }>();
  const row = await c.env.DB.prepare("SELECT station_id FROM station_added_segments WHERE id = ?")
    .bind(aid).first<{ station_id: number }>();
  if (!row) return c.notFound();
  const manual = JSON.stringify({
    intervals: body.intervals ?? [],
    singles: body.singles ?? [],
    whole: body.whole ?? false,
    bez_broja: !!body.bez_broja,
  });
  await c.env.DB.prepare("UPDATE station_added_segments SET manual_json = ? WHERE id = ?")
    .bind(manual, aid).run();
  await markDirty(c.env.DB, row.station_id);
  return c.json({ ok: true });
});

app.delete("/api/added/:addedId", async (c) => {
  const aid = Number(c.req.param("addedId"));
  const row = await c.env.DB.prepare("SELECT station_id FROM station_added_segments WHERE id = ?")
    .bind(aid).first<{ station_id: number }>();
  if (!row) return c.notFound();
  await c.env.DB.prepare("DELETE FROM station_added_segments WHERE id = ?").bind(aid).run();
  await markDirty(c.env.DB, row.station_id);
  return c.json({ ok: true });
});

// ── Station-level edits (worker-owned; consumed by the pipeline reconcile step) ──────────

// Correct an existing station's raw coverage text. The pipeline re-parses it into fresh
// segments on the next recompute; because segment ids are positional, that invalidates any
// manual segment edits on this station — so purge them here (see memory reparse-stale-overrides).
app.put("/api/s/:id/text", async (c) => {
  const id = Number(c.req.param("id"));
  const body = await c.req.json<{ raw_coverage_text?: string }>();
  const text = (body.raw_coverage_text ?? "").trim();
  if (isAddedStationId(id)) {
    // Added stations keep their text in added_stations, not a separate override row.
    const aid = id - ADDED_STATION_BASE;
    const row = await c.env.DB.prepare("SELECT id FROM added_stations WHERE id = ?").bind(aid).first();
    if (!row) return c.notFound();
    await c.env.DB.prepare("UPDATE added_stations SET raw_coverage_text = ? WHERE id = ?")
      .bind(text, aid).run();
    await markDirty(c.env.DB, id);
    return c.json({ ok: true });
  }
  const st = await c.env.DB.prepare("SELECT id FROM polling_stations WHERE id = ?").bind(id).first();
  if (!st) return c.notFound();
  await c.env.DB.prepare(
    `INSERT INTO station_text_overrides (station_id, raw_coverage_text, updated_at)
     VALUES (?1, ?2, datetime('now'))
     ON CONFLICT(station_id) DO UPDATE SET raw_coverage_text = ?2, updated_at = datetime('now')`
  ).bind(id, text).run();
  // Re-parse will renumber this station's segments — drop now-stale overrides.
  await c.env.DB.prepare(
    `DELETE FROM segment_overrides WHERE segment_id IN
       (SELECT id FROM coverage_segments WHERE station_id = ?)`
  ).bind(id).run();
  await markDirty(c.env.DB, id);
  return c.json({ ok: true });
});

app.delete("/api/s/:id/text", async (c) => {
  const id = Number(c.req.param("id"));
  const st = await c.env.DB.prepare("SELECT id FROM polling_stations WHERE id = ?").bind(id).first();
  if (!st) return c.notFound();
  await c.env.DB.prepare("DELETE FROM station_text_overrides WHERE station_id = ?").bind(id).run();
  await markDirty(c.env.DB, id);
  return c.json({ ok: true });
});

// Add a brand-new station the document omitted. Coverage is entered as raw text and parsed by
// the pipeline reconcile step. Returns the synthetic station id so the UI can open it.
app.post("/api/m/:id/stations", async (c) => {
  const muniId = c.req.param("id");
  const body = await c.req.json<{
    name_cyr?: string; address_cyr?: string; number?: number; raw_coverage_text?: string;
  }>();
  const name = (body.name_cyr ?? "").trim();
  if (!name) return c.json({ ok: false, error: "name_cyr required" }, 400);
  const muni = await c.env.DB.prepare("SELECT id FROM municipalities WHERE id = ?").bind(muniId).first();
  if (!muni) return c.json({ ok: false, error: "unknown municipality" }, 400);
  // Auto-assign the next printed number within the muni when none is given (covers both real
  // and previously-added stations).
  let number = body.number ?? null;
  if (number == null) {
    const row = await c.env.DB.prepare(
      `SELECT MAX(n) AS n FROM (
         SELECT MAX(number) AS n FROM polling_stations WHERE municipality_id = ?1
         UNION ALL SELECT MAX(number) AS n FROM added_stations WHERE municipality_id = ?1)`
    ).bind(muniId).first<{ n: number | null }>();
    number = (row?.n ?? 0) + 1;
  }
  const res = await c.env.DB.prepare(
    `INSERT INTO added_stations (municipality_id, number, name_cyr, address_cyr, raw_coverage_text, created_at)
     VALUES (?, ?, ?, ?, ?, datetime('now'))`
  ).bind(muniId, number, name, body.address_cyr ?? null, (body.raw_coverage_text ?? "").trim()).run();
  const stationId = ADDED_STATION_BASE + Number(res.meta.last_row_id);
  await markDirty(c.env.DB, stationId);
  return c.json({ ok: true, station_id: stationId });
});

// Edit a reviewer-added station's metadata (name / address / number).
app.put("/api/s/:id/added", async (c) => {
  const id = Number(c.req.param("id"));
  if (!isAddedStationId(id)) return c.json({ ok: false, error: "not an added station" }, 400);
  const aid = id - ADDED_STATION_BASE;
  const body = await c.req.json<{ name_cyr?: string; address_cyr?: string; number?: number }>();
  const row = await c.env.DB.prepare("SELECT id FROM added_stations WHERE id = ?").bind(aid).first();
  if (!row) return c.notFound();
  await c.env.DB.prepare(
    `UPDATE added_stations SET
       name_cyr = COALESCE(?2, name_cyr),
       address_cyr = ?3,
       number = COALESCE(?4, number)
     WHERE id = ?1`
  ).bind(aid, body.name_cyr?.trim() || null, body.address_cyr?.trim() ?? null, body.number ?? null).run();
  await markDirty(c.env.DB, id);
  return c.json({ ok: true });
});

app.delete("/api/s/:id/added", async (c) => {
  const id = Number(c.req.param("id"));
  if (!isAddedStationId(id)) return c.json({ ok: false, error: "not an added station" }, 400);
  await c.env.DB.prepare("DELETE FROM added_stations WHERE id = ?").bind(id - ADDED_STATION_BASE).run();
  await markDirty(c.env.DB, id);
  return c.json({ ok: true });
});

// Remove (tombstone) / restore an existing station.
app.post("/api/s/:id/remove", async (c) => {
  const id = Number(c.req.param("id"));
  const body = await c.req.json<{ reason?: string }>().catch(() => ({ reason: undefined }));
  const st = await c.env.DB.prepare("SELECT id FROM polling_stations WHERE id = ?").bind(id).first();
  if (!st) return c.notFound();
  await c.env.DB.prepare(
    `INSERT INTO removed_stations (station_id, reason, removed_at)
     VALUES (?1, ?2, datetime('now'))
     ON CONFLICT(station_id) DO UPDATE SET reason = ?2, removed_at = datetime('now')`
  ).bind(id, body.reason ?? null).run();
  await markDirty(c.env.DB, id);
  return c.json({ ok: true });
});

app.delete("/api/s/:id/remove", async (c) => {
  const id = Number(c.req.param("id"));
  await c.env.DB.prepare("DELETE FROM removed_stations WHERE station_id = ?").bind(id).run();
  await markDirty(c.env.DB, id);
  return c.json({ ok: true });
});

app.get("/api/s/:id/streets", async (c) => {
  const script = getScript(c);
  const q = (c.req.query("q") ?? "").trim();
  if (q.length < 2) return c.json([]);
  const rows = await searchStreets(c.env.DB, Number(c.req.param("id")), q);
  return c.json(rows.map((r) => ({
    id: r.id,
    name: script === "lat" ? r.name_lat : r.name_cyr,
    settlement: script === "lat" ? r.settlement_lat : r.settlement_cyr,
    area: !!r.area,
  })));
});

app.get("/api/m/:id/polygons.geojson", async (c) => {
  const script = getScript(c);
  const [rows, bounds, removed] = await Promise.all([
    allMuniPolygons(c.env.POLY, c.req.param("id")),
    muniBoundaries(c.env.DB, c.req.param("id")),
    removedStationIds(c.env.DB, c.req.param("id")),
  ]);
  return c.json({
    type: "FeatureCollection",
    features: rows.filter((r) => !removed.has(r.station_id)).map((r) => ({
      type: "Feature",
      geometry: JSON.parse(r.geojson),
      properties: {
        station_id: r.station_id,
        number: r.number,
        name: script === "lat" ? r.name_lat : r.name_cyr,
        address: script === "lat" ? r.address_lat : r.address_cyr,
      },
    })),
    boundaries: bounds.map((b) => JSON.parse(b.geojson)),
  });
});

// Downloadable per-municipality coverage exports. Shape mirrors
// data/exports/novi-beograd-automated.geojson (OKRUG/RBR omitted — not in our data).
app.get("/api/m/:id/export.geojson", async (c) => {
  const script = getScript(c);
  const id = c.req.param("id");
  const [muni, rows, removed] = await Promise.all([
    getMunicipality(c.env.DB, id), allMuniPolygons(c.env.POLY, id), removedStationIds(c.env.DB, id),
  ]);
  if (!muni) return c.notFound();
  const kept = rows.filter((r) => !removed.has(r.station_id));
  const body = JSON.stringify(buildGeoJSON(kept as ExportRow[], muni.name_cyr, script));
  c.header("Content-Type", "application/geo+json; charset=utf-8");
  c.header("Content-Disposition", `attachment; filename="${muniSlug(muni.name_lat)}.geojson"`);
  return c.body(body);
});

app.get("/api/m/:id/export.kml", async (c) => {
  const script = getScript(c);
  const id = c.req.param("id");
  const [muni, rows, removed] = await Promise.all([
    getMunicipality(c.env.DB, id), allMuniPolygons(c.env.POLY, id), removedStationIds(c.env.DB, id),
  ]);
  if (!muni) return c.notFound();
  const body = buildKML(rows.filter((r) => !removed.has(r.station_id)) as ExportRow[], muni.name_cyr, script);
  c.header("Content-Type", "application/vnd.google-earth.kml+xml; charset=utf-8");
  c.header("Content-Disposition", `attachment; filename="${muniSlug(muni.name_lat)}.kml"`);
  return c.body(body);
});

// All municipality outlines for the homepage overview. Static register data, so it is
// the one response allowed to escape the global no-store (browser cache for a day).
app.get("/api/munis/boundaries.geojson", async (c) => {
  const rows = await allBoundaries(c.env.DB);
  c.header("Cache-Control", "public, max-age=86400");
  return c.json({
    type: "FeatureCollection",
    features: rows.map((r) => ({
      type: "Feature",
      geometry: JSON.parse(r.geojson),
      properties: { municipality_id: r.municipality_id },
    })),
  });
});

app.get("/api/s/:id/points.geojson", async (c) => {
  return c.json(await pointsForStation(c.env.DB, Number(c.req.param("id"))));
});

app.get("/api/s/:id/street-lines.geojson", async (c) => {
  return c.json(await streetLinesForStation(c.env.DB, Number(c.req.param("id"))));
});

app.get("/api/s/:id/polygon.geojson", async (c) => {
  const id = Number(c.req.param("id"));
  const st = await getStation(c.env.DB, id);
  // One R2 read of the muni blob yields this station's polygon AND its neighbours.
  const [rows, bounds, removed] = st
    ? await Promise.all([
        allMuniPolygons(c.env.POLY, st.municipality_id),
        muniBoundaries(c.env.DB, st.municipality_id),
        removedStationIds(c.env.DB, st.municipality_id),
      ])
    : [[], [], new Set<number>()];
  const self = rows.find((r) => r.station_id === id);
  const neighbors = rows.filter((r) => r.station_id !== id && !removed.has(r.station_id));
  return c.json({
    polygon: self ? JSON.parse(self.geojson) : null,
    meta: self ? { area_m2: self.area_m2, point_count: self.point_count, computed_at: self.computed_at } : null,
    neighbors: neighbors.map((n) => JSON.parse(n.geojson)),
    boundaries: bounds.map((b) => JSON.parse(b.geojson)),
    // Per-segment OSM-fallback shapes (no addresses): the UI draws/zooms them by segment_id.
    osm: self?.osm ? JSON.parse(self.osm) : [],
  });
});

app.get("/api/health", async (c) => {
  const row = await c.env.DB.prepare("SELECT COUNT(*) AS n FROM polling_stations").first<{ n: number }>();
  return c.json({ ok: true, stations: row?.n ?? 0 });
});

async function markDirty(db: D1Database, stationId: number) {
  await db.prepare(
    `INSERT INTO station_status (station_id, dirty, updated_at)
     VALUES (?, 1, datetime('now'))
     ON CONFLICT(station_id) DO UPDATE SET dirty = 1, updated_at = datetime('now')`
  ).bind(stationId).run();
}

export default app;
