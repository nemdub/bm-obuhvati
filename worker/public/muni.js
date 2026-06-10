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
