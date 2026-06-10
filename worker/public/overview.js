/* Homepage overview map: every municipality outline, colored by review status. */
(() => {
  const cfg = JSON.parse(document.getElementById("cfg").textContent);
  const map = L.map("overview-map");
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap",
  }).addTo(map);
  map.setView([44.2, 20.9], 7);

  const OK = "#2e7d32";
  const WARN = "#ef6c00";
  const byId = new Map(cfg.munis.map((m) => [m.id, m]));

  fetch("/api/munis/boundaries.geojson")
    .then((r) => r.json())
    .then((fc) => {
      const layer = L.geoJSON(fc, {
        style: (f) => {
          const m = byId.get(f.properties.municipality_id);
          if (!m) return { color: "#9aa3ab", weight: 1, fillColor: "#9aa3ab", fillOpacity: 0.1 };
          const col = m.review > 0 ? WARN : OK;
          return { color: "#5a6670", weight: 1, fillColor: col, fillOpacity: 0.3 };
        },
        onEachFeature: (f, l) => {
          const m = byId.get(f.properties.municipality_id);
          if (!m) return;
          l.bindTooltip(`${m.name} · ${m.stations} ${cfg.labels.stations} · ${m.review} ${cfg.labels.needsReview}`);
          l.on("mouseover", () => l.setStyle({ weight: 2.5, fillOpacity: 0.5 }));
          l.on("mouseout", () => l.setStyle({ weight: 1, fillOpacity: 0.3 }));
          l.on("click", () => { window.location.href = `/m/${m.id}`; });
        },
      }).addTo(map);
      const b = layer.getBounds();
      if (b.isValid()) map.fitBounds(b, { padding: [10, 10] });
    });
})();
