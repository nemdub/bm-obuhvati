import { html, raw } from "hono/html";
import type { Context } from "hono";
import { makeT } from "./i18n";
import { tr, type Script } from "./translit";
import type { MunicipalityRow, StationRow } from "./db";

export function getScript(c: Context): Script {
  const q = c.req.query("script");
  if (q === "lat" || q === "cyr") return q;
  const cookie = c.req.header("Cookie") ?? "";
  return /(?:^|;\s*)script=lat/.test(cookie) ? "lat" : "cyr";
}

function layout(script: Script, titleText: string, body: unknown) {
  const t = makeT(script);
  const other: Script = script === "cyr" ? "lat" : "cyr";
  return html`<!doctype html>
<html lang="sr">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>${titleText}</title>
<link rel="stylesheet" href="/app.css" />
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
</head>
<body>
<header class="topbar">
  <a class="brand" href="/">${t("appTitle")}</a>
  <span class="sub">${t("appSubtitle")}</span>
  <a class="script-toggle" href="?script=${other}">${other === "lat" ? "Latinica" : "Ћирилица"}</a>
</header>
<main>${body}</main>
</body>
</html>`;
}

export function municipalitiesView(c: Context, munis: MunicipalityRow[]) {
  const script = getScript(c);
  const t = makeT(script);
  const rows = munis.map(
    (m) => html`<tr>
      <td><a href="/m/${m.id}">${tr(m.name_cyr, script)}</a></td>
      <td class="num">${m.station_count}</td>
      <td class="num">${m.review_count > 0 ? html`<span class="badge warn">${m.review_count}</span>` : "0"}</td>
    </tr>`
  );
  return layout(
    script,
    t("appTitle"),
    html`<h1>${t("municipalities")}</h1>
      <table class="list">
        <thead><tr><th>${t("municipality")}</th><th class="num">${t("stations")}</th><th class="num">${t("needsReview")}</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`
  );
}

export function stationsView(
  c: Context,
  muni: { id: string; name_cyr: string },
  stations: any[]
) {
  const script = getScript(c);
  const t = makeT(script);
  const rows = stations.map(
    (s) => html`<tr>
      <td class="num">${s.number}</td>
      <td><a href="/s/${s.id}">${tr(s.name_cyr, script)}</a>
        ${s.is_amendment ? html`<span class="badge amend">${t("amendment")}</span>` : ""}</td>
      <td class="num">${s.seg_count}</td>
      <td class="num">${s.review_count > 0 ? html`<span class="badge warn">${s.review_count}</span>` : "0"}</td>
      <td class="num">${s.has_polygon ? "▰" : "—"}</td>
      <td class="num">${s.reviewed ? html`<span class="badge ok">✓</span>` : ""}${s.dirty ? html`<span class="badge dirty">⟳</span>` : ""}</td>
    </tr>`
  );
  return layout(
    script,
    tr(muni.name_cyr, script),
    html`<p class="crumb"><a href="/">${t("municipalities")}</a> › ${tr(muni.name_cyr, script)}</p>
      <h1>${t("stations")}</h1>
      <div id="muni-map"></div>
      <table class="list" id="stations-table">
        <thead><tr>
          <th class="num sortable" data-col="0">${t("number")} <span class="arrow"></span></th>
          <th>${t("name")}</th>
          <th class="num">${t("segments")}</th>
          <th class="num sortable" data-col="3">${t("needsReview")} <span class="arrow"></span></th>
          <th class="num">${t("polygon")}</th><th class="num">${t("reviewed")}</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <script id="cfg" type="application/json">${raw(JSON.stringify({ muniId: muni.id }))}</script>
      <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
      <script src="/muni.js"></script>`
  );
}

export function stationDetailView(c: Context, st: StationRow, muniName: string) {
  const script = getScript(c);
  const t = makeT(script);
  const name = tr(st.name_cyr, script);
  const addr = tr(st.address_cyr, script);
  const coverage = tr(st.raw_coverage_text, script);
  const cfg = JSON.stringify({ stationId: st.id, muniId: st.municipality_id, script });
  return layout(
    script,
    name,
    html`<p class="crumb"><a href="/">${t("municipalities")}</a> › <a href="/m/${st.municipality_id}">${tr(muniName, script)}</a> › #${st.number}</p>
      <div class="detail">
        <section class="panel">
          <h1>#${st.number} · ${name}</h1>
          <p class="addr">${addr}</p>
          <div class="actions">
            <button id="recompute" class="btn">${t("recompute")}</button>
            <span id="status-msg" class="status-msg"></span>
          </div>
          <div class="source">
            <div class="source-label">${t("rawText")}</div>
            <div class="source-text">${coverage}</div>
          </div>
          <h2>${t("segments")}</h2>
          <div id="segments" class="segments"></div>
        </section>
        <section class="mapwrap">
          <div id="poly-meta" class="polymeta"></div>
          <div id="map"></div>
        </section>
      </div>
      <script id="cfg" type="application/json">${raw(cfg)}</script>
      <script>window.__L = ${raw(JSON.stringify(labelBundle(script)))};</script>
      <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
      <script src="/app.js"></script>`
  );
}

/** Labels app.js needs, pre-transliterated for the active script. */
function labelBundle(script: Script) {
  const t = makeT(script);
  return {
    street: t("street"), wholeStreet: t("wholeStreet"), ranges: t("ranges"),
    singles: t("singles"), suffix: t("suffix"), save: t("save"), revert: t("revert"),
    markReviewed: t("markReviewed"), needsReview: t("needsReview"), reviewed: t("reviewed"),
    confidence: t("confidence"), amendmentNote: t("amendmentNote"), streetUnresolved: t("streetUnresolved"),
    addRange: t("addRange"), addSingle: t("addSingle"), saved: t("saved"), matchedAddresses: t("matchedAddresses"),
    noPointsForStreet: t("noPointsForStreet"),
    parityAll: t("parityAll"), parityOdd: t("parityOdd"), parityEven: t("parityEven"),
    reviewReason: t("reviewReason"),
    polygon: t("polygon"), noPolygon: t("noPolygon"), stale: t("stale"), recomputeQueued: t("recomputeQueued"),
    source: t("source"), base: t("base"), amendment: t("amendment"),
  };
}
