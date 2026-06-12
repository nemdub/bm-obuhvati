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

  let pointsLayer = null, polygonLayer = null, neighborsLayer = null, boundaryLayer = null, streetLinesLayer = null;
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

  // Street centerlines for streets with no addresses (no points to draw): faint by
  // default, highlighted when their segment is focused.
  function streetLineStyle(f) {
    const active = highlightSeg && f.properties.segment_id === highlightSeg;
    const dim = highlightSeg && !active;
    return {
      color: active ? "#1565c0" : "#ef6c00",
      weight: active ? 5 : 3,
      opacity: dim ? 0.3 : active ? 1 : 0.7,
      dashArray: active ? null : "5 5",
    };
  }
  function refreshStreetLines() {
    if (streetLinesLayer) streetLinesLayer.setStyle(streetLineStyle);
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

  // Highlight a segment's points (and street line, if it has no addresses) and zoom to
  // them; null clears + fits all points.
  function focusSegment(segId) {
    refreshPoints();
    refreshStreetLines();
    if (segId == null) {
      const b = pointsLayer && pointsLayer.getBounds();
      if (b && b.isValid()) map.fitBounds(b, { padding: [30, 30] });
      return;
    }
    const bounds = L.latLngBounds([]);
    if (pointsLayer) pointsLayer.eachLayer((l) => {
      if (l.feature && l.feature.properties.segment_id === segId) bounds.extend(l.getLatLng());
    });
    // Streets with no addresses have no points — fall back to their stored centerline.
    if (streetLinesLayer) streetLinesLayer.eachLayer((l) => {
      if (l.feature && l.feature.properties.segment_id === segId) bounds.extend(l.getBounds());
    });
    if (bounds.isValid()) {
      map.fitBounds(bounds, { padding: [40, 40], maxZoom: 17 });
    } else {
      toast(L_.noPointsForStreet);
    }
  }

  async function loadMap() {
    const [pts, poly, lines] = await Promise.all([
      fetch(api("/points.geojson")).then((r) => r.json()),
      fetch(api("/polygon.geojson")).then((r) => r.json()),
      fetch(api("/street-lines.geojson")).then((r) => r.json()),
    ]);

    if (boundaryLayer) map.removeLayer(boundaryLayer);
    boundaryLayer = L.geoJSON({ type: "FeatureCollection", features: (poly.boundaries || []).map((g) => ({ type: "Feature", geometry: g })) },
      { style: { color: "#616161", weight: 1.5, dashArray: "6 4", fill: false }, interactive: false }).addTo(map);

    if (neighborsLayer) map.removeLayer(neighborsLayer);
    neighborsLayer = L.geoJSON({ type: "FeatureCollection", features: (poly.neighbors || []).map((g) => ({ type: "Feature", geometry: g })) },
      { style: { color: "#9e9e9e", weight: 1, fillOpacity: 0.04, dashArray: "3" } }).addTo(map);

    if (polygonLayer) map.removeLayer(polygonLayer);
    polygonLayer = poly.polygon
      ? L.geoJSON({ type: "Feature", geometry: poly.polygon }, { style: { color: "#1565c0", weight: 2, fillOpacity: 0.12 } }).addTo(map)
      : null;

    if (streetLinesLayer) map.removeLayer(streetLinesLayer);
    streetLinesLayer = L.geoJSON(lines, { style: streetLineStyle }).addTo(map);

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
    else if (streetLinesLayer && streetLinesLayer.getBounds().isValid())
      map.fitBounds(streetLinesLayer.getBounds(), { padding: [30, 30] });
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

  // Picker result label: streets show their settlement, settlements show an area tag.
  function streetItemLabel(r) {
    return r.area ? `${r.name} — ${L_.settlementArea}` : `${r.name} (${r.settlement})`;
  }

  // Reviewer-added street claim card: street name + numbers, with a remove button only
  // (the document never mentions this street; the claim itself is the human statement).
  function renderAddedSegment(seg) {
    const card = document.createElement("div");
    card.className = "seg manual";
    const head = document.createElement("div");
    head.className = "seg-head";
    head.innerHTML = `<span class="seg-title">${escapeHtml(seg.street_name)}</span>` +
      `<span class="badge ok">${L_.addedBadge}</span>`;
    head.onclick = () => { highlightSeg = highlightSeg === seg.id ? null : seg.id; focusSegment(highlightSeg); };
    const body = document.createElement("div");
    body.className = "seg-body";
    const desc = document.createElement("p");
    desc.className = "amend-note";
    desc.textContent = seg.parsed.whole ? L_.wholeStreet :
      JSON.stringify({ [L_.ranges]: seg.parsed.intervals, [L_.singles]: seg.parsed.singles });
    body.appendChild(desc);
    const actions = document.createElement("div");
    actions.className = "seg-actions";
    actions.appendChild(mkBtn(L_.removeAdded, "btn", async () => {
      await fetch(`/api/added/${seg.added_id}`, { method: "DELETE" });
      await reload();
    }));
    body.appendChild(actions);
    card.append(head, body);
    return card;
  }

  // "Add street" panel: search the register, add a whole-street claim to this station.
  function renderAddPanel() {
    const wrap = document.createElement("div");
    wrap.className = "seg add-panel";
    const head = document.createElement("div");
    head.className = "seg-head";
    head.innerHTML = `<span class="seg-title">+ ${L_.addStreet}</span>`;
    const body = document.createElement("div");
    body.className = "seg-body";
    body.style.display = "none";
    head.onclick = () => { body.style.display = body.style.display === "none" ? "block" : "none"; };
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = L_.searchStreet;
    input.className = "street-search";
    const list = document.createElement("div");
    list.className = "street-results";
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = input.value.trim();
        if (q.length < 2) { list.innerHTML = ""; return; }
        const rows = await fetch(api(`/streets?q=${encodeURIComponent(q)}`)).then((r) => r.json());
        list.innerHTML = "";
        rows.forEach((r) => {
          const item = document.createElement("div");
          item.className = "street-item" + (r.area ? " area" : "");
          item.textContent = streetItemLabel(r);
          item.onclick = async () => {
            await fetch(api("/segments"), {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ street_id: r.id, whole: true }),
            });
            await reload();
          };
          list.appendChild(item);
        });
      }, 250);
    });
    body.append(input, list);
    wrap.append(head, body);
    return wrap;
  }

  function renderSegment(seg) {
    if (seg.added_id) return renderAddedSegment(seg);
    const card = document.createElement("div");
    card.className = "seg" + (seg.needs_review ? " review" : "") + (seg.manual_locked ? " manual" : "");

    const head = document.createElement("div");
    head.className = "seg-head";
    const title = seg.street_resolved ? (seg.street_name || seg.street_raw) : seg.street_raw;
    head.innerHTML = `<span class="seg-title">${escapeHtml(title)}</span>`;
    if (seg.street_missing) head.innerHTML += `<span class="badge ok">${L_.streetMissing}</span>`;
    else if (!seg.street_resolved) head.innerHTML += `<span class="badge warn">${L_.streetUnresolved}</span>`;
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

    // Street picker: lets the reviewer (re)assign the register street — crucial when the
    // street is unresolved or matched to the wrong one. chosenStreet rides along on save.
    let chosenStreet = null; // street id picked in this card session
    const sp = document.createElement("div");
    sp.className = "street-pick";
    const spBtn = mkBtn(L_.changeStreet, "mini add", () => {
      spBox.style.display = spBox.style.display === "none" ? "block" : "none";
      if (spBox.style.display === "block") spInput.focus();
    });
    const spBox = document.createElement("div");
    // Open by default when unresolved, but not once it's been marked "doesn't exist".
    spBox.style.display = seg.street_resolved || seg.street_missing ? "none" : "block";
    const spInput = document.createElement("input");
    spInput.type = "text";
    spInput.placeholder = L_.searchStreet;
    spInput.className = "street-search";
    const spList = document.createElement("div");
    spList.className = "street-results";
    let spTimer = null;
    spInput.addEventListener("input", () => {
      clearTimeout(spTimer);
      spTimer = setTimeout(async () => {
        const q = spInput.value.trim();
        if (q.length < 2) { spList.innerHTML = ""; return; }
        const rows = await fetch(api(`/streets?q=${encodeURIComponent(q)}`)).then((r) => r.json());
        spList.innerHTML = "";
        rows.forEach((r) => {
          const item = document.createElement("div");
          item.className = "street-item" + (r.area ? " area" : "");
          item.textContent = streetItemLabel(r);
          item.onclick = () => {
            chosenStreet = r.id;
            spInput.value = streetItemLabel(r);
            spList.innerHTML = "";
          };
          spList.appendChild(item);
        });
      }, 250);
    });
    if (seg.manual_street) {
      const note = document.createElement("span");
      note.className = "badge ok";
      note.textContent = L_.manualStreetSet;
      sp.appendChild(note);
    }
    spBox.append(spInput, spList);
    sp.append(spBtn, spBox);
    body.appendChild(sp);

    // whole-street toggle
    const wholeLbl = document.createElement("label");
    wholeLbl.className = "whole";
    const whole = document.createElement("input");
    whole.type = "checkbox";
    whole.checked = !!seg.parsed.whole;
    wholeLbl.appendChild(whole);
    wholeLbl.appendChild(document.createTextNode(" " + L_.wholeStreet));
    body.appendChild(wholeLbl);

    // bez-broja ("бб") toggle: also cover the street's no-number houses
    const bezLbl = document.createElement("label");
    bezLbl.className = "whole";
    const bezBroja = document.createElement("input");
    bezBroja.type = "checkbox";
    bezBroja.checked = !!seg.parsed.bez_broja;
    bezLbl.appendChild(bezBroja);
    bezLbl.appendChild(document.createTextNode(" " + L_.bezBroja));
    body.appendChild(bezLbl);

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
    const payload = (reviewedFlag) => JSON.stringify({
      ...collect(whole, bezBroja, ivList, sgList, reviewedFlag),
      street_id: chosenStreet ?? seg.manual_street_id ?? null, // keep prior manual street
    });
    const save = mkBtn(L_.save, "btn primary", async () => {
      await fetch(`/api/segments/${seg.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: payload(false),
      });
      flash(actions, L_.saved);
      await reload();
    });
    const reviewed = mkBtn(L_.markReviewed, "btn", async () => {
      await fetch(`/api/segments/${seg.id}`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: payload(true),
      });
      await reload();
    });
    actions.appendChild(save);
    actions.appendChild(reviewed);
    // "Doesn't exist": for an unmatched street, confirm it's absent from the register
    // and resolve the segment (no addresses/polygon are built for it).
    if (!seg.street_resolved && !seg.street_missing) {
      actions.appendChild(mkBtn(L_.doesNotExist, "btn", async () => {
        await fetch(`/api/segments/${seg.id}`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...collect(whole, bezBroja, ivList, sgList, true), street_id: "none" }),
        });
        await reload();
      }));
    }
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
  function boundInput(num, sfx, cls) {
    // Text input holding a bound like "23" or "23Ц" (suffix-bounded range edge).
    const i = document.createElement("input");
    i.type = "text";
    i.className = cls;
    i.value = num == null || num === "" ? "" : `${num}${sfx || ""}`;
    return i;
  }
  function parseBound(v) {
    const m = (v || "").trim().match(/^(\d+)\s*[-/]?\s*(.*)$/);
    if (!m) return [0, ""];
    return [Number(m[1]), (m[2] || "").trim().toUpperCase()];
  }
  function intervalRow(iv) {
    const row = document.createElement("div");
    row.className = "row iv";
    const lo = boundInput(iv[0], iv.length > 3 ? iv[3] : "", "lo");
    const hi = boundInput(iv[1], iv.length > 4 ? iv[4] : "", "hi");
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
  function collect(whole, bezBroja, ivList, sgList, reviewed) {
    const intervals = [...ivList.querySelectorAll(".row.iv")].map((r) => {
      const [lo, loSfx] = parseBound(r.querySelector(".lo").value);
      const [hi, hiSfx] = parseBound(r.querySelector(".hi").value);
      const parity = r.querySelector(".parity").value;
      return loSfx || hiSfx ? [lo, hi, parity, loSfx, hiSfx] : [lo, hi, parity];
    }).filter((x) => x[0] || x[1]);
    const singles = [...sgList.querySelectorAll(".row.sg")].map((r) =>
      [Number(r.querySelector(".n").value), r.querySelector(".suffix").value.trim().toUpperCase()]
    ).filter((x) => x[0] || x[1]);
    return { whole: whole.checked, bez_broja: bezBroja.checked, intervals, singles, reviewed };
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
    wrap.appendChild(renderAddPanel());
    segs.forEach((s) => wrap.appendChild(renderSegment(s)));
  }

  async function reload() {
    await Promise.all([renderSegments(), loadMap()]);
  }

  reload();
})();
