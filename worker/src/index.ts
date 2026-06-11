import { Hono } from "hono";
import type { Env } from "./types";
import { tr } from "./translit";
import { REVIEW_REASONS } from "./i18n";
import {
  listMunicipalities, getMunicipality, listStations, getStation, getSegments,
  getPolygon, pointsForStation, muniPolygons, allMuniPolygons, effectiveParsed, searchStreets,
  muniBoundaries, allBoundaries, summaryStats,
} from "./db";
import { getScript, municipalitiesView, stationsView, stationDetailView } from "./views";

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
  const [munis, stats] = await Promise.all([listMunicipalities(c.env.DB), summaryStats(c.env.DB)]);
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
      return {
        id: s.id,
        street_raw: tr(s.street_raw, script),
        street_name: s.ov_street_name_cyr
          ? tr(s.ov_street_name_cyr, script)
          : settlementClaim
            ? tr(s.street_raw, script)
            : s.street_name_cyr ? tr(s.street_name_cyr, script) : null,
        street_resolved: !!(s.ov_street_id ?? s.street_id),
        manual_street: !!s.ov_street_id,
        manual_street_id: s.ov_street_id,
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
            if ((base === "fuzzy" || base === "muni_fallback" || base === "alias") && s.street_id) {
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
    reviewed?: boolean; street_id?: string | null;
  }>();
  const manual = JSON.stringify({
    intervals: body.intervals ?? [],
    singles: body.singles ?? [],
    whole: !!body.whole,
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
    street_id: string; whole?: boolean;
    intervals?: unknown[]; singles?: unknown[];
  }>();
  if (!body.street_id) return c.json({ ok: false, error: "street_id required" }, 400);
  const street = await c.env.DB.prepare("SELECT id FROM streets WHERE id = ?")
    .bind(body.street_id).first();
  if (!street) return c.json({ ok: false, error: "unknown street" }, 400);
  const manual = JSON.stringify({
    intervals: body.intervals ?? [],
    singles: body.singles ?? [],
    whole: body.whole ?? true,
  });
  await c.env.DB.prepare(
    `INSERT INTO station_added_segments (station_id, street_id, manual_json, created_at)
     VALUES (?, ?, ?, datetime('now'))`
  ).bind(stationId, body.street_id, manual).run();
  await markDirty(c.env.DB, stationId);
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

app.get("/api/s/:id/streets", async (c) => {
  const script = getScript(c);
  const q = (c.req.query("q") ?? "").trim();
  if (q.length < 2) return c.json([]);
  const rows = await searchStreets(c.env.DB, Number(c.req.param("id")), q);
  return c.json(rows.map((r) => ({
    id: r.id,
    name: script === "lat" ? r.name_lat : r.name_cyr,
    settlement: script === "lat" ? r.settlement_lat : r.settlement_cyr,
  })));
});

app.get("/api/m/:id/polygons.geojson", async (c) => {
  const script = getScript(c);
  const [rows, bounds] = await Promise.all([
    allMuniPolygons(c.env.DB, c.req.param("id")),
    muniBoundaries(c.env.DB, c.req.param("id")),
  ]);
  return c.json({
    type: "FeatureCollection",
    features: rows.map((r) => ({
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

app.get("/api/s/:id/polygon.geojson", async (c) => {
  const id = Number(c.req.param("id"));
  const poly = await getPolygon(c.env.DB, id);
  const st = await getStation(c.env.DB, id);
  const [neighbors, bounds] = st
    ? await Promise.all([
        muniPolygons(c.env.DB, st.municipality_id, id),
        muniBoundaries(c.env.DB, st.municipality_id),
      ])
    : [[], []];
  return c.json({
    polygon: poly ? JSON.parse(poly.geojson) : null,
    meta: poly ? { area_m2: poly.area_m2, point_count: poly.point_count, computed_at: poly.computed_at } : null,
    neighbors: neighbors.map((n) => JSON.parse(n.geojson)),
    boundaries: bounds.map((b) => JSON.parse(b.geojson)),
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
