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

  // "+ Add station": reveal the form, POST a new (reviewer-added) station, open its page.
  const addBtn = document.getElementById("add-station-btn");
  const addForm = document.getElementById("add-station-form");
  if (addBtn && addForm) {
    addBtn.addEventListener("click", () => {
      addForm.style.display = addForm.style.display === "none" ? "block" : "none";
    });
    document.getElementById("as-cancel").addEventListener("click", () => {
      addForm.style.display = "none";
    });
    document.getElementById("as-save").addEventListener("click", async () => {
      const name = document.getElementById("as-name").value.trim();
      if (!name) { document.getElementById("as-name").focus(); return; }
      const numRaw = document.getElementById("as-number").value.trim();
      const res = await fetch(`/api/m/${cfg.muniId}/stations`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name_cyr: name,
          address_cyr: document.getElementById("as-address").value.trim() || null,
          number: numRaw ? Number(numRaw) : null,
          raw_coverage_text: document.getElementById("as-text").value.trim(),
        }),
      }).then((r) => r.json());
      if (res && res.station_id) window.location.href = `/s/${res.station_id}`;
    });
  }

  // Sortable stations table: by polling-station number (default) and checks-needed.
  const table = document.getElementById("stations-table");
  if (table && table.tBodies.length) {
    const tbody = table.tBodies[0];
    let sortCol = 0;
    let sortDir = 1; // 1 = ascending
    const cellNum = (row, col) => parseFloat(row.cells[col].textContent) || 0;
    const isHead = (r) => r.classList.contains("section-head");
    const applySort = () => {
      // Sort WITHIN each section block (a member town's sub-table — Kostolac/Sevojno — keeps
      // its own numbering), leaving the divider rows in their block order. Municipalities
      // without sections form a single group, so this is the plain sort.
      const groups = [];
      let cur = null;
      for (const r of [...tbody.rows]) {
        if (isHead(r)) { cur = { head: r, items: [] }; groups.push(cur); }
        else { if (!cur) { cur = { head: null, items: [] }; groups.push(cur); } cur.items.push(r); }
      }
      for (const g of groups) {
        g.items.sort((a, b) => (cellNum(a, sortCol) - cellNum(b, sortCol)) * sortDir);
        if (g.head) tbody.appendChild(g.head);
        g.items.forEach((r) => tbody.appendChild(r));
      }
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
      let boundaryLayer = null;
      if (fc.boundaries && fc.boundaries.length) {
        boundaryLayer = L.geoJSON(
          { type: "FeatureCollection", features: fc.boundaries.map((g) => ({ type: "Feature", geometry: g })) },
          { style: { color: "#616161", weight: 1.5, dashArray: "6 4", fill: false }, interactive: false }
        ).addTo(map);
      }
      const layer = L.geoJSON(fc, {
        style: (f) => ({ color: color(f.properties.number), weight: 1, fillOpacity: 0.25, fillColor: color(f.properties.number) }),
        onEachFeature: (f, l) => {
          l.bindTooltip(
            `#${f.properties.number} · ${f.properties.name}` +
            (f.properties.address ? `<br><span class="tt-addr">${f.properties.address}</span>` : "")
          );
          l.on("mouseover", () => l.setStyle({ weight: 3, fillOpacity: 0.45 }));
          l.on("mouseout", () => l.setStyle({ weight: 1, fillOpacity: 0.25 }));
          l.on("click", () => { window.location.href = `/s/${f.properties.station_id}`; });
        },
      }).addTo(map);
      const b = layer.getBounds();
      if (boundaryLayer) b.extend(boundaryLayer.getBounds());
      if (b.isValid()) map.fitBounds(b, { padding: [20, 20] });
    });
})();
