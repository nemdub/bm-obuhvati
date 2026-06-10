/* bm-obuhvati review UI: Leaflet map + coverage-segment editor (vanilla JS). */
(() => {
  const cfg = JSON.parse(document.getElementById("cfg").textContent);
  const L_ = window.__L; // pre-transliterated labels
  const api = (p) => `/api/s/${cfg.stationId}${p}`;

  const map = L.map("map");
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap',
  }).addTo(map);
  map.setView([44.0, 20.9], 7);

  let pointsLayer = null, polygonLayer = null, neighborsLayer = null;
  let highlightSeg = null;

  function pointStyle(f) {
    const p = f.properties;
    let color = "#2e7d32"; // confident
    if (p.needs_review) color = "#ef6c00";
    else if (p.confidence < 0.8) color = "#f9a825";
    const active = highlightSeg && p.segment_id === highlightSeg;
    const dim = highlightSeg && !active;
    return {
      radius: active ? 6 : 4,
      color: active ? "#1565c0" : color,
      weight: active ? 2 : 1,
      fillOpacity: dim ? 0.2 : 0.85,
      opacity: dim ? 0.25 : 1,
      fillColor: color,
    };
  }

  let toastTimer = null;
  function toast(msg) {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 2200);
  }

  // Highlight a segment's points and zoom to them; null clears + fits all points.
  function focusSegment(segId) {
    refreshPoints();
    if (!pointsLayer) return;
    if (segId == null) {
      const b = pointsLayer.getBounds();
      if (b.isValid()) map.fitBounds(b, { padding: [30, 30] });
      return;
    }
    const bounds = L.latLngBounds([]);
    pointsLayer.eachLayer((l) => {
      if (l.feature && l.feature.properties.segment_id === segId) bounds.extend(l.getLatLng());
    });
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 17 });
    } else {
      toast(L_.noPointsForStreet);
    }
  }

  async function loadMap() {
    const [pts, poly] = await Promise.all([
      fetch(api("/points.geojson")).then((r) => r.json()),
      fetch(api("/polygon.geojson")).then((r) => r.json()),
    ]);

    if (neighborsLayer) map.removeLayer(neighborsLayer);
    neighborsLayer = L.geoJSON({ type: "FeatureCollection", features: (poly.neighbors || []).map((g) => ({ type: "Feature", geometry: g })) },
      { style: { color: "#9e9e9e", weight: 1, fillOpacity: 0.04, dashArray: "3" } }).addTo(map);

    if (polygonLayer) map.removeLayer(polygonLayer);
    polygonLayer = poly.polygon
      ? L.geoJSON({ type: "Feature", geometry: poly.polygon }, { style: { color: "#1565c0", weight: 2, fillOpacity: 0.12 } }).addTo(map)
      : null;

    if (pointsLayer) map.removeLayer(pointsLayer);
    pointsLayer = L.geoJSON(pts, {
      pointToLayer: (f, latlng) => L.circleMarker(latlng, pointStyle(f)).bindTooltip(f.properties.house),
    }).addTo(map);

    const meta = document.getElementById("poly-meta");
    if (poly.meta) {
      const km2 = (poly.meta.area_m2 / 1e6).toFixed(2);
      meta.textContent = `${L_.matchedAddresses}: ${poly.meta.point_count} · ${L_.polygon}: ${km2} km²`;
    } else {
      meta.textContent = L_.noPolygon;
    }

    const b = pointsLayer.getBounds();
    if (b.isValid()) map.fitBounds(b, { padding: [30, 30] });
    else if (polygonLayer) map.fitBounds(polygonLayer.getBounds(), { padding: [30, 30] });
  }

  function refreshPoints() {
    if (!pointsLayer) return;
    pointsLayer.setStyle((f) => pointStyle(f));
  }

  function numInput(v, cls) {
    const i = document.createElement("input");
    i.type = cls === "suffix" ? "text" : "number";
    i.className = cls;
    i.value = v ?? "";
    return i;
  }

  function rowRemoveBtn(onClick) {
    const b = document.createElement("button");
    b.className = "mini";
    b.textContent = "×";
    b.onclick = onClick;
    return b;
  }

  function renderSegment(seg) {
    const card = document.createElement("div");
    card.className = "seg" + (seg.needs_review ? " review" : "") + (seg.manual_locked ? " manual" : "");

    const head = document.createElement("div");
    head.className = "seg-head";
    const title = seg.street_resolved ? (seg.street_name || seg.street_raw) : seg.street_raw;
    head.innerHTML = `<span class="seg-title">${escapeHtml(title)}</span>`;
    if (!seg.street_resolved) head.innerHTML += `<span class="badge warn">${L_.streetUnresolved}</span>`;
    if (seg.source === "amendment") head.innerHTML += `<span class="badge amend">${L_.amendment}</span>`;
    if (seg.needs_review) head.innerHTML += `<span class="badge warn">${L_.needsReview}</span>`;
    if (seg.manual_locked) head.innerHTML += `<span class="badge ok">✎</span>`;
    head.onclick = () => { highlightSeg = highlightSeg === seg.id ? null : seg.id; focusSegment(highlightSeg); };
    card.appendChild(head);

    const body = document.createElement("div");
    body.className = "seg-body";

    if (seg.needs_review && seg.review_reasons && seg.review_reasons.length) {
      const rr = document.createElement("div");
      rr.className = "review-reasons";
      rr.innerHTML = `<span class="rr-label">${L_.reviewReason}:</span>`;
      const ul = document.createElement("ul");
      seg.review_reasons.forEach((reason) => {
        const li = document.createElement("li");
        li.textContent = reason;
        ul.appendChild(li);
      });
      rr.appendChild(ul);
      body.appendChild(rr);
    }

    if (seg.amendment_note) {
      const note = document.createElement("p");
      note.className = "amend-note";
      note.textContent = `${L_.amendmentNote}: ${seg.amendment_note}`;
      body.appendChild(note);
    }

    // whole-street toggle
    const wholeLbl = document.createElement("label");
    wholeLbl.className = "whole";
    const whole = document.createElement("input");
    whole.type = "checkbox";
    whole.checked = !!seg.parsed.whole;
    wholeLbl.appendChild(whole);
    wholeLbl.appendChild(document.createTextNode(" " + L_.wholeStreet));
    body.appendChild(wholeLbl);

    // intervals
    const ivWrap = document.createElement("div");
    ivWrap.className = "field";
    ivWrap.innerHTML = `<span class="flabel">${L_.ranges}</span>`;
    const ivList = document.createElement("div");
    (seg.parsed.intervals || []).forEach((iv) => ivList.appendChild(intervalRow(iv)));
    const addIv = document.createElement("button");
    addIv.className = "mini add";
    addIv.textContent = "+ " + L_.addRange;
    addIv.onclick = () => ivList.appendChild(intervalRow([0, 0]));
    ivWrap.appendChild(ivList);
    ivWrap.appendChild(addIv);
    body.appendChild(ivWrap);

    // singles
    const sgWrap = document.createElement("div");
    sgWrap.className = "field";
    sgWrap.innerHTML = `<span class="flabel">${L_.singles}</span>`;
    const sgList = document.createElement("div");
    (seg.parsed.singles || []).forEach((s) => sgList.appendChild(singleRow(s)));
    const addSg = document.createElement("button");
    addSg.className = "mini add";
    addSg.textContent = "+ " + L_.addSingle;
    addSg.onclick = () => sgList.appendChild(singleRow([0, ""]));
    sgWrap.appendChild(sgList);
    sgWrap.appendChild(addSg);
    body.appendChild(sgWrap);

    // actions
    const actions = document.createElement("div");
    actions.className = "seg-actions";
    const save = mkBtn(L_.save, "btn primary", async () => {
      await fetch(`/api/segments/${seg.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collect(whole, ivList, sgList, false)),
      });
      flash(actions, L_.saved);
      await reload();
    });
    const reviewed = mkBtn(L_.markReviewed, "btn", async () => {
      await fetch(`/api/segments/${seg.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(collect(whole, ivList, sgList, true)),
      });
      await reload();
    });
    actions.appendChild(save);
    actions.appendChild(reviewed);
    if (seg.manual_locked) {
      actions.appendChild(mkBtn(L_.revert, "btn", async () => {
        await fetch(`/api/segments/${seg.id}/manual`, { method: "DELETE" });
        await reload();
      }));
    }
    body.appendChild(actions);
    card.appendChild(body);
    return card;
  }

  function impliedParity(lo, hi) {
    if (lo % 2 === 1 && hi % 2 === 1) return "odd";
    if (lo % 2 === 0 && hi % 2 === 0) return "even";
    return "all";
  }
  function intervalRow(iv) {
    const row = document.createElement("div");
    row.className = "row iv";
    const lo = numInput(iv[0], "lo"), hi = numInput(iv[1], "hi");
    const par = document.createElement("select");
    par.className = "parity";
    [["all", L_.parityAll], ["odd", L_.parityOdd], ["even", L_.parityEven]].forEach(([v, label]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = label;
      par.appendChild(o);
    });
    par.value = iv.length > 2 && iv[2] ? iv[2] : impliedParity(iv[0] || 0, iv[1] || 0);
    row.append(lo, document.createTextNode("–"), hi, par, rowRemoveBtn(() => row.remove()));
    return row;
  }
  function singleRow(s) {
    const row = document.createElement("div");
    row.className = "row sg";
    const n = numInput(s[0], "n"), suf = numInput(s[1], "suffix");
    suf.placeholder = L_.suffix;
    row.append(n, suf, rowRemoveBtn(() => row.remove()));
    return row;
  }
  function collect(whole, ivList, sgList, reviewed) {
    const intervals = [...ivList.querySelectorAll(".row.iv")].map((r) =>
      [Number(r.querySelector(".lo").value), Number(r.querySelector(".hi").value), r.querySelector(".parity").value]
    ).filter((x) => x[0] || x[1]);
    const singles = [...sgList.querySelectorAll(".row.sg")].map((r) =>
      [Number(r.querySelector(".n").value), r.querySelector(".suffix").value.trim().toUpperCase()]
    ).filter((x) => x[0] || x[1]);
    return { whole: whole.checked, intervals, singles, reviewed };
  }

  function mkBtn(text, cls, onClick) {
    const b = document.createElement("button");
    b.className = cls;
    b.textContent = text;
    b.onclick = onClick;
    return b;
  }
  function flash(el, msg) {
    const s = document.createElement("span");
    s.className = "flash";
    s.textContent = msg;
    el.appendChild(s);
    setTimeout(() => s.remove(), 1500);
  }
  function escapeHtml(s) {
    return (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  async function renderSegments() {
    const segs = await fetch(api("/segments")).then((r) => r.json());
    const wrap = document.getElementById("segments");
    wrap.innerHTML = "";
    segs.forEach((s) => wrap.appendChild(renderSegment(s)));
  }

  async function reload() {
    await Promise.all([renderSegments(), loadMap()]);
  }

  document.getElementById("recompute").onclick = async () => {
    await fetch(api("/recompute"), { method: "POST" });
    document.getElementById("status-msg").textContent = L_.recomputeQueued;
  };

  reload();
})();
