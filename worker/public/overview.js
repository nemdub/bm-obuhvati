/* Homepage overview map: every municipality outline, colored by review status. */
(() => {
  const cfg = JSON.parse(document.getElementById("cfg").textContent);
  const map = L.map("overview-map");
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap",
  }).addTo(map);
  map.setView([44.2, 20.9], 7);

  // Fill colour signals how many checks a municipality still needs: green = none,
  // escalating through yellow/orange to red as the backlog grows.
  const SCALE = [
    { min: 0, color: "#2e7d32", label: "0" },
    { min: 1, color: "#c0ca33", label: "1–25" },
    { min: 26, color: "#fbc02d", label: "26–50" },
    { min: 51, color: "#ef6c00", label: "51–100" },
    { min: 101, color: "#c62828", label: "100+" },
  ];
  const reviewColor = (n) => {
    let c = SCALE[0].color;
    for (const s of SCALE) if (n >= s.min) c = s.color;
    return c;
  };
  const byId = new Map(cfg.munis.map((m) => [m.id, m]));

  // Legend keyed to the checks-needed colour scale.
  const legend = L.control({ position: "bottomright" });
  legend.onAdd = () => {
    const div = L.DomUtil.create("div", "map-legend");
    div.innerHTML =
      `<div class="legend-title">${cfg.labels.needsReview}</div>` +
      SCALE.map(
        (s) => `<div class="legend-row"><span class="legend-swatch" style="background:${s.color}"></span>${s.label}</div>`
      ).join("");
    return div;
  };
  legend.addTo(map);

  fetch("/api/munis/boundaries.geojson")
    .then((r) => r.json())
    .then((fc) => {
      const layer = L.geoJSON(fc, {
        style: (f) => {
          const m = byId.get(f.properties.municipality_id);
          if (!m) return { color: "#9aa3ab", weight: 1, fillColor: "#9aa3ab", fillOpacity: 0.1 };
          return { color: "#5a6670", weight: 1, fillColor: reviewColor(m.review), fillOpacity: 0.45 };
        },
        onEachFeature: (f, l) => {
          const m = byId.get(f.properties.municipality_id);
          if (!m) return;
          l.bindTooltip(`${m.name} · ${m.stations} ${cfg.labels.stations} · ${m.review} ${cfg.labels.needsReview}`);
          l.on("mouseover", () => l.setStyle({ weight: 2.5, fillOpacity: 0.65 }));
          l.on("mouseout", () => l.setStyle({ weight: 1, fillOpacity: 0.45 }));
          l.on("click", () => { window.location.href = `/m/${m.id}`; });
        },
      }).addTo(map);
      const b = layer.getBounds();
      if (b.isValid()) map.fitBounds(b, { padding: [10, 10] });
    });
})();
