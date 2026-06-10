import { html, raw } from "hono/html";
import type { Context } from "hono";
import { makeT } from "./i18n";
import { tr, type Script } from "./translit";
import { srCyrillicCompare } from "./collate";
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

// Cities whose city-municipalities each have their own stations/coverage but are nested
// under a city header in the list for navigation (display only — no scope/coverage change).
const CITY_DISPLAY = [
  {
    cyr: "Београд", lat: "Beograd",
    ids: new Set(["70181", "70254", "71293", "70114", "70092", "70203", "70157", "70165",
      "70246", "70122", "70211", "70173", "70149", "70106", "70238", "70190", "70220"]),
  },
  {
    cyr: "Ниш", lat: "Niš",
    ids: new Set(["71331", "71323", "71307", "71315", "71285"]), // Medijana, Palilula(Niš), Pantelej, Crveni krst, Niška Banja
  },
];
const GROUPED_IDS = new Set(CITY_DISPLAY.flatMap((c) => [...c.ids]));

function muniRow(m: MunicipalityRow, script: Script, indent = false) {
  return html`<tr class="${indent ? "city-child" : ""}">
    <td><a href="/m/${m.id}">${tr(m.name_cyr, script)}</a></td>
    <td class="num">${m.station_count}</td>
    <td class="num">${m.review_count > 0 ? html`<span class="badge warn">${m.review_count}</span>` : "0"}</td>
  </tr>`;
}

export function municipalitiesView(c: Context, munis: MunicipalityRow[]) {
  const script = getScript(c);
  const t = makeT(script);

  // Each entry is a standalone municipality or a nested city group; all sorted together by
  // Serbian Cyrillic azbuka (a city group sorts at its city name).
  type Entry = { sortName: string; html: unknown };
  const entries: Entry[] = munis
    .filter((m) => !GROUPED_IDS.has(m.id))
    .map((m) => ({ sortName: m.name_cyr, html: muniRow(m, script) }));

  for (const city of CITY_DISPLAY) {
    const members = munis.filter((m) => city.ids.has(m.id));
    if (!members.length) continue;
    members.sort((a, b) => srCyrillicCompare(a.name_cyr, b.name_cyr));
    const stations = members.reduce((s, m) => s + m.station_count, 0);
    const review = members.reduce((s, m) => s + m.review_count, 0);
    entries.push({
      sortName: city.cyr,
      html: html`<tr class="city-head">
          <td>${script === "lat" ? city.lat : city.cyr}</td>
          <td class="num">${stations}</td>
          <td class="num">${review > 0 ? html`<span class="badge warn">${review}</span>` : "0"}</td>
        </tr>
        ${members.map((m) => muniRow(m, script, true))}`,
    });
  }
  entries.sort((a, b) => srCyrillicCompare(a.sortName, b.sortName));

  return layout(
    script,
    t("appTitle"),
    html`<h1>${t("municipalities")}</h1>
      <table class="list">
        <thead><tr><th>${t("municipality")}</th><th class="num">${t("stations")}</th><th class="num">${t("needsReview")}</th></tr></thead>
        <tbody>${entries.map((e) => e.html)}</tbody>
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
      <td class="num">${s.has_polygon ? html`<span class="ok-mark">✓</span>` : html`<span class="badge warn">${t("none")}</span>`}</td>
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
    changeStreet: t("changeStreet"), searchStreet: t("searchStreet"), manualStreetSet: t("manualStreetSet"),
    polygon: t("polygon"), noPolygon: t("noPolygon"), stale: t("stale"), recomputeQueued: t("recomputeQueued"),
    source: t("source"), base: t("base"), amendment: t("amendment"),
  };
}
