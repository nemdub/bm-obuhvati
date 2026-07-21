"""Build a small self-contained web page to review the combined volunteer/automated GeoJSON files.

Writes two files into ``data/volunteer-combined/`` next to the ``*_combined.geojson`` outputs:
  - ``manifest.json`` — one entry per municipality (file, name, counts, cleanup notes) so the page
    knows what to load; built from ``combine_stats.json`` + ``data_quality.json``.
  - ``index.html``    — a Leaflet viewer: pick / step through municipalities, polygons coloured by
    source (volunteer / automated fallback / volunteer-only), source layer toggles, and a
    checked/flagged progress tracker (persisted in the browser) so you can sweep all of them.

Serve the folder over HTTP (the page fetches the GeoJSON by relative path):
  cd data/volunteer-combined && python3 -m http.server 8791   # then open http://localhost:8791/

Run AFTER combine_volunteer.py:  .venv/bin/python pipeline/build_combined_viewer.py
"""

from __future__ import annotations

import json

import config
from volunteer_combine_summary import cleanup_flags

OUT_DIR = config.VOLUNTEER_COMBINE_DIR
EXPORT_DIR = config.VOLUNTEER_COMBINED_OUT


def build_manifest() -> list[dict]:
    stats = json.loads((OUT_DIR / "combine_stats.json").read_text())
    dq = json.loads((OUT_DIR / "data_quality.json").read_text())
    dq_by_pid: dict[str, list[dict]] = {}
    for d in dq:
        if d["polygon_muni_id"]:
            dq_by_pid.setdefault(d["polygon_muni_id"], []).append(d)

    entries = []
    for s in stats:
        pid = s["polygon_muni_id"]
        entries.append({
            "pid": pid,
            "name": s.get("name_lat", pid),
            "opstina": s.get("opstina", ""),
            "file": s["output_file"],
            "source": s["source"],
            "features": s["n_features"],
            "vol_usable": s["n_vol_usable"],
            "auto_total": s["n_auto_total"],
            "completeness": s["completeness"],
            "reason": s["reason"],
            "cleanup": cleanup_flags(dq_by_pid[pid]) if pid in dq_by_pid else "no volunteer data",
        })
    entries.sort(key=lambda e: e["name"])
    return entries


def main() -> None:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest()
    (EXPORT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    (EXPORT_DIR / "index.html").write_text(INDEX_HTML)
    print(f"Wrote viewer for {len(manifest)} municipalities -> "
          f"{(EXPORT_DIR / 'index.html').relative_to(config.ROOT_DIR)}")
    print("Serve it:  cd data/volunteer-combined && python3 -m http.server 8791  "
          "# open http://localhost:8791/")


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="sr">
<head>
<meta charset="utf-8">
<title>Combined coverage review</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  :root { --vol:#2e7d32; --auto:#1769aa; --extra:#f9a825; }
  * { box-sizing: border-box; }
  html,body { height:100%; margin:0; font:14px/1.4 system-ui, sans-serif; }
  #app { display:flex; height:100%; }
  #side { width:300px; flex:0 0 300px; border-right:1px solid #ddd; display:flex; flex-direction:column; background:#fafafa; }
  #side h1 { font-size:14px; margin:0; padding:10px 12px; border-bottom:1px solid #ddd; background:#fff; }
  #prog { padding:6px 12px; font-size:12px; color:#555; border-bottom:1px solid #eee; }
  #filter { margin:8px 12px; padding:6px 8px; width:calc(100% - 24px); border:1px solid #ccc; border-radius:4px; }
  #list { flex:1; overflow-y:auto; }
  .item { padding:7px 12px; border-bottom:1px solid #eee; cursor:pointer; display:flex; align-items:center; gap:6px; }
  .item:hover { background:#eef; }
  .item.active { background:#dde7ff; }
  .item.checked { opacity:.55; }
  .item .nm { flex:1; font-weight:500; }
  .item.checked .nm { text-decoration:line-through; }
  .badge { font-size:10px; padding:1px 5px; border-radius:8px; color:#fff; }
  .b-v { background:var(--vol); } .b-a { background:var(--auto); } .b-e { background:var(--extra); }
  .warn { color:#c62828; font-weight:bold; }
  .flagged .nm::after { content:" \1F6A9"; }
  #main { flex:1; display:flex; flex-direction:column; min-width:0; }
  #bar { padding:8px 12px; border-bottom:1px solid #ddd; display:flex; align-items:center; gap:10px; flex-wrap:wrap; background:#fff; }
  #bar .title { font-size:16px; font-weight:600; }
  #bar .meta { font-size:12px; color:#555; }
  #bar button { padding:5px 10px; border:1px solid #bbb; background:#f4f4f4; border-radius:4px; cursor:pointer; }
  #bar button:hover { background:#e8e8e8; }
  #bar .spacer { flex:1; }
  .laytog { font-size:12px; display:inline-flex; align-items:center; gap:3px; }
  #note { font-size:12px; color:#a15c00; }
  #map { flex:1; }
  .legend { background:#fff; padding:6px 9px; font-size:12px; line-height:1.5; box-shadow:0 1px 4px rgba(0,0,0,.3); border-radius:4px; }
  .legend i { display:inline-block; width:11px; height:11px; margin-right:5px; vertical-align:middle; opacity:.6; }
  kbd { background:#eee; border:1px solid #ccc; border-bottom-width:2px; border-radius:3px; padding:0 4px; font-size:11px; }
</style>
</head>
<body>
<div id="app">
  <div id="side">
    <h1>Combined coverage review</h1>
    <div id="prog"></div>
    <input id="filter" placeholder="filter municipalities…">
    <div id="list"></div>
  </div>
  <div id="main">
    <div id="bar">
      <button id="prev" title="Previous (←)">‹ Prev</button>
      <button id="next" title="Next (→)">Next ›</button>
      <span class="title" id="title">—</span>
      <span class="badge" id="srcbadge"></span>
      <span class="meta" id="counts"></span>
      <span class="spacer"></span>
      <button id="chk" title="Toggle checked (c)">✓ Checked</button>
      <button id="flg" title="Toggle flag (f)">🚩 Flag</button>
    </div>
    <div id="note"></div>
    <div id="map"></div>
  </div>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const COLORS = { volunteer:'#2e7d32', automatic:'#1769aa' };
const styleFor = s => ({
  color: COLORS[s] || '#777', weight: 2, fillColor: COLORS[s] || '#777',
  fillOpacity: s === 'automatic' ? 0.15 : 0.30, dashArray: s === 'automatic' ? '4 3' : null,
});
const LS = 'combined-review-state';
const state = JSON.parse(localStorage.getItem(LS) || '{"checked":{},"flagged":{}}');
const save = () => localStorage.setItem(LS, JSON.stringify(state));

let manifest = [], view = [], cur = -1, layer = null;

const map = L.map('map').setView([44.0, 20.9], 7);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  { maxZoom:19, attribution:'© OpenStreetMap' }).addTo(map);
const legend = L.control({ position:'bottomright' });
legend.onAdd = () => { const d = L.DomUtil.create('div','legend');
  d.innerHTML = '<i style="background:#2e7d32"></i>volunteer muni<br>'+
    '<i style="background:#1769aa"></i>automated muni (fallback)'; return d; };
legend.addTo(map);

const el = id => document.getElementById(id);

function renderList() {
  const q = el('filter').value.trim().toLowerCase();
  view = manifest.filter(m => !q || m.name.toLowerCase().includes(q) || m.pid.includes(q));
  const box = el('list'); box.innerHTML = '';
  view.forEach((m, i) => {
    const d = document.createElement('div');
    d.className = 'item' + (state.checked[m.pid] ? ' checked' : '') + (state.flagged[m.pid] ? ' flagged' : '');
    d.dataset.i = i;
    const hasCleanupIssue = m.cleanup !== 'clean' && m.cleanup !== 'no volunteer data';
    const warn = hasCleanupIssue ? '<span class="warn" title="'+m.cleanup+'">!</span>' : '';
    const cls = m.source === 'automatic' ? 'b-a' : 'b-v';
    const lbl = m.source === 'automatic' ? 'AUTO' : 'VOL';
    d.innerHTML = `<span class="nm">${m.name}</span>${warn}`+
      `<span class="badge ${cls}" title="${m.reason}">${lbl} ${m.features}</span>`;
    d.onclick = () => select(i);
    box.appendChild(d);
  });
  updateProgress();
  highlight();
}

function updateProgress() {
  const n = manifest.filter(m => state.checked[m.pid]).length;
  const f = manifest.filter(m => state.flagged[m.pid]).length;
  el('prog').innerHTML = `Checked <b>${n}</b>/${manifest.length}` + (f ? ` · 🚩 ${f} flagged` : '') +
    `<br><kbd>←</kbd><kbd>→</kbd> nav · <kbd>c</kbd> check · <kbd>f</kbd> flag`;
}

function highlight() {
  document.querySelectorAll('.item').forEach(d => d.classList.toggle('active', +d.dataset.i === cur));
}

async function select(i) {
  if (i < 0 || i >= view.length) return;
  cur = i; highlight();
  const m = view[i];
  el('title').textContent = m.name + '  (' + m.pid + ')';
  const badge = el('srcbadge');
  badge.textContent = (m.source === 'automatic' ? 'AUTOMATED' : 'VOLUNTEER');
  badge.style.background = COLORS[m.source] || '#777';
  el('counts').textContent =
    `${m.features} polygons · completeness ${Math.round(m.completeness*100)}% ` +
    `(${m.vol_usable}/${m.auto_total} volunteer/stations)`;
  el('note').textContent = (m.source === 'automatic' ? '⚠ Automated — ' + m.reason + '. ' : '') +
    (m.cleanup !== 'clean' && m.cleanup !== 'no volunteer data' ? 'Cleanup: ' + m.cleanup : '');
  el('chk').style.background = state.checked[m.pid] ? '#c8e6c9' : '';
  el('flg').style.background = state.flagged[m.pid] ? '#ffcdd2' : '';
  if (layer) { map.removeLayer(layer); layer = null; }
  try {
    const gj = await (await fetch(m.file)).json();
    layer = L.geoJSON(gj, {
      style: f => styleFor(f.properties.source),
      onEachFeature: (f, l) => { const p = f.properties;
        l.bindPopup(`<b>${p.NAZIV_BM || p.Uparivanje || '—'}</b><br>`+
          `source: <b style="color:${COLORS[p.source]}">${p.source}</b><br>`+
          `BR_BM: ${p.BR_BM ?? '—'}` + (p.vol_br_bm!=null && p.vol_br_bm!==p.BR_BM ? ` (vol #${p.vol_br_bm})` : '') + `<br>`+
          `priority: ${p.priority ?? '—'} · voters: ${p.voters ?? '—'}<br>`+
          `${p.ADRESA_BM || ''}`); },
    }).addTo(map);
    const b = layer.getBounds();
    if (b.isValid()) map.fitBounds(b, { padding:[20,20] });
  } catch (e) { el('note').textContent = 'Failed to load ' + m.file + ': ' + e; }
}

function toggle(kind) {
  if (cur < 0) return;
  const pid = view[cur].pid;
  state[kind][pid] = !state[kind][pid]; if (!state[kind][pid]) delete state[kind][pid];
  save(); renderList(); select(cur);
}

el('prev').onclick = () => select(cur - 1);
el('next').onclick = () => select(cur + 1);
el('chk').onclick = () => toggle('checked');
el('flg').onclick = () => toggle('flagged');
el('filter').oninput = renderList;
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowLeft') select(cur - 1);
  else if (e.key === 'ArrowRight') select(cur + 1);
  else if (e.key === 'c') toggle('checked');
  else if (e.key === 'f') toggle('flagged');
});

fetch('manifest.json').then(r => r.json()).then(m => {
  manifest = m; renderList();
  if (view.length) select(0);
});
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
