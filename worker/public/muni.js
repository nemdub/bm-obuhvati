/* Municipality overview map: all polling-station coverage polygons at a glance. */
(() => {
  const cfg = JSON.parse(document.getElementById("cfg").textContent);
  const map = L.map("muni-map");
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap",
  }).addTo(map);
  map.setView([44.0, 20.9], 7);

  // Distinct-ish palette cycled by station number so neighbors are easy to tell apart.
  const PALETTE = [
    "#1565c0", "#2e7d32", "#ef6c00", "#6a1b9a", "#c62828", "#00838f",
    "#558b2f", "#ad1457", "#4527a0", "#00695c", "#e65100", "#283593",
  ];
  const color = (n) => PALETTE[((n % PALETTE.length) + PALETTE.length) % PALETTE.length];

  // Sortable stations table: by polling-station number (default) and checks-needed.
  const table = document.getElementById("stations-table");
  if (table && table.tBodies.length) {
    const tbody = table.tBodies[0];
    let sortCol = 0;
    let sortDir = 1; // 1 = ascending
    const cellNum = (row, col) => parseFloat(row.cells[col].textContent) || 0;
    const applySort = () => {
      [...tbody.rows]
        .sort((a, b) => (cellNum(a, sortCol) - cellNum(b, sortCol)) * sortDir)
        .forEach((r) => tbody.appendChild(r));
      table.querySelectorAll("th.sortable .arrow").forEach((a) => (a.textContent = ""));
      const arrow = table.querySelector(`th.sortable[data-col="${sortCol}"] .arrow`);
      if (arrow) arrow.textContent = sortDir > 0 ? "▲" : "▼";
    };
    table.querySelectorAll("th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const col = Number(th.dataset.col);
        if (col === sortCol) sortDir = -sortDir;
        else { sortCol = col; sortDir = 1; }
        applySort();
      });
    });
    applySort(); // default: number ascending
  }

  fetch(`/api/m/${cfg.muniId}/polygons.geojson`)
    .then((r) => r.json())
    .then((fc) => {
      const layer = L.geoJSON(fc, {
        style: (f) => ({ color: color(f.properties.number), weight: 1, fillOpacity: 0.25, fillColor: color(f.properties.number) }),
        onEachFeature: (f, l) => {
          l.bindTooltip(`#${f.properties.number} · ${f.properties.name}`);
          l.on("mouseover", () => l.setStyle({ weight: 3, fillOpacity: 0.45 }));
          l.on("mouseout", () => l.setStyle({ weight: 1, fillOpacity: 0.25 }));
          l.on("click", () => { window.location.href = `/s/${f.properties.station_id}`; });
        },
      }).addTo(map);
      const b = layer.getBounds();
      if (b.isValid()) map.fitBounds(b, { padding: [20, 20] });
    });
})();
