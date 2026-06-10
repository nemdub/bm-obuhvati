import { Hono } from "hono";
import type { Env } from "./types";
import { tr } from "./translit";
import { REVIEW_REASONS } from "./i18n";
import {
  listMunicipalities, getMunicipality, listStations, getStation, getSegments,
  getPolygon, pointsForStation, muniPolygons, allMuniPolygons, effectiveParsed,
} from "./db";
import { getScript, municipalitiesView, stationsView, stationDetailView } from "./views";
import { srLatinCompare } from "./collate";

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
app.get("/", async (c) => {
  const munis = await listMunicipalities(c.env.DB);
  munis.sort((a, b) => srLatinCompare(a.name_lat, b.name_lat)); // Serbian abeceda order
  return c.html(municipalitiesView(c, munis));
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
    segs.map((s) => ({
      id: s.id,
      street_raw: tr(s.street_raw, script),
      street_name: s.street_name_cyr ? tr(s.street_name_cyr, script) : null,
      street_resolved: !!s.street_id,
      kind: s.kind,
      parsed: effectiveParsed(s),
      manual_locked: s.manual_locked,
      confidence: s.confidence,
      needs_review: s.needs_review,
      review_reasons: (s.review_reason ?? "")
        .split(",")
        .filter(Boolean)
        .map((code) => {
          let text = tr(REVIEW_REASONS[code] ?? code, script);
          // For name-based matches, spell out document spelling -> matched register name
          // (the card title shows the resolved name, so the discrepancy isn't otherwise visible).
          if ((code === "fuzzy" || code === "muni_fallback") && s.street_id) {
            const matched = (script === "lat" ? s.street_name_lat : s.street_name_cyr) ?? "";
            text += `: „${tr(s.street_raw, script)}“ → „${matched}“`;
          }
          return text;
        }),
      source: s.source,
      amendment_note: s.amendment_note ? tr(s.amendment_note, script) : null,
    }))
  );
});

app.put("/api/segments/:id", async (c) => {
  const segId = Number(c.req.param("id"));
  const body = await c.req.json<{ intervals?: [number, number][]; singles?: [number, string][]; whole?: boolean; reviewed?: boolean }>();
  const manual = JSON.stringify({
    intervals: body.intervals ?? [],
    singles: body.singles ?? [],
    whole: !!body.whole,
  });
  const reviewClause = body.reviewed ? ", needs_review = 0" : "";
  const seg = await c.env.DB.prepare("SELECT station_id FROM coverage_segments WHERE id = ?")
    .bind(segId).first<{ station_id: number }>();
  if (!seg) return c.notFound();
  await c.env.DB.prepare(
    `UPDATE coverage_segments SET manual_json = ?, manual_locked = 1${reviewClause} WHERE id = ?`
  ).bind(manual, segId).run();
  await markDirty(c.env.DB, seg.station_id);
  return c.json({ ok: true });
});

app.delete("/api/segments/:id/manual", async (c) => {
  const segId = Number(c.req.param("id"));
  const seg = await c.env.DB.prepare("SELECT station_id FROM coverage_segments WHERE id = ?")
    .bind(segId).first<{ station_id: number }>();
  if (!seg) return c.notFound();
  await c.env.DB.prepare(
    "UPDATE coverage_segments SET manual_json = NULL, manual_locked = 0 WHERE id = ?"
  ).bind(segId).run();
  await markDirty(c.env.DB, seg.station_id);
  return c.json({ ok: true });
});

app.get("/api/m/:id/polygons.geojson", async (c) => {
  const script = getScript(c);
  const rows = await allMuniPolygons(c.env.DB, c.req.param("id"));
  return c.json({
    type: "FeatureCollection",
    features: rows.map((r) => ({
      type: "Feature",
      geometry: JSON.parse(r.geojson),
      properties: {
        station_id: r.station_id,
        number: r.number,
        name: script === "lat" ? r.name_lat : r.name_cyr,
      },
    })),
  });
});

app.get("/api/s/:id/points.geojson", async (c) => {
  return c.json(await pointsForStation(c.env.DB, Number(c.req.param("id"))));
});

app.get("/api/s/:id/polygon.geojson", async (c) => {
  const id = Number(c.req.param("id"));
  const poly = await getPolygon(c.env.DB, id);
  const st = await getStation(c.env.DB, id);
  const neighbors = st ? await muniPolygons(c.env.DB, st.municipality_id, id) : [];
  return c.json({
    polygon: poly ? JSON.parse(poly.geojson) : null,
    meta: poly ? { area_m2: poly.area_m2, point_count: poly.point_count, computed_at: poly.computed_at } : null,
    neighbors: neighbors.map((n) => JSON.parse(n.geojson)),
  });
});

app.post("/api/s/:id/recompute", async (c) => {
  await markDirty(c.env.DB, Number(c.req.param("id")));
  return c.json({ ok: true });
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
