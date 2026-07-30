/* SG Property Tracker — map + time filter + per-property history.
   Vanilla JS, no build step. Reads data.json produced by `python -m ingest.run`.

   Encoding: colour = median price per sqft (sequential, one hue, fixed scale);
   shape = source (circle private / rounded square HDB). Two channels, two jobs —
   so the time filter changes a marker's value without ever changing what its
   shape means. */

(() => {
  "use strict";

  const SG_CENTRE = [1.3521, 103.8198];
  const PSF_STEPS = 5;

  const $ = (id) => document.getElementById(id);
  const el = {
    meta: $("meta"), map: $("map"), legend: $("legend"), legendScale: $("legendScale"),
    stage: document.querySelector(".stage"), hoverCard: $("hoverCard"),
    panel: $("panel"), panelBody: $("panelBody"), panelClose: $("panelClose"),
    scrim: $("scrim"), rangeStart: $("rangeStart"), rangeEnd: $("rangeEnd"),
    rangeLabel: $("rangeLabel"), drFill: $("drFill"), play: $("play"),
    playGlyph: $("playGlyph"), playText: $("playText"), reset: $("reset"),
    moreFilters: $("moreFilters"), moreToggle: $("moreToggle"),
    filterCount: $("filterCount"), emptyNote: $("emptyNote"),
    minSqft: $("minSqft"), maxSqft: $("maxSqft"),
    minPrice: $("minPrice"), maxPrice: $("maxPrice"),
    minLease: $("minLease"), leaseLabel: $("leaseLabel"), leaseFill: $("leaseFill"),
    modelChips: $("modelChips"), sourceChips: $("sourceChips"),
    schoolsToggle: $("schoolsToggle"), legendSchool: $("legendSchool"),
    compareToggle: $("compareToggle"), compareBar: $("compareBar"),
    compareSearch: $("compareSearch"), compareResults: $("compareResults"),
    compareSlots: $("compareSlots"), compareHint: $("compareHint"),
    compareCount: $("compareCount"), comparePanel: $("comparePanel"),
    compareBody: $("compareBody"), compareClear: $("compareClear"),
    compareClose: $("compareClose"), cmpScope: $("cmpScope"),
    cmpModeChips: $("cmpModeChips"), cmpChartTitle: $("cmpChartTitle"),
    cmpChartSub: $("cmpChartSub"),
    presets: $("presets"),
  };

  const state = {
    properties: [], months: [], thresholds: [],
    source: "ALL", startIdx: 0, endIdx: 0,
    // null means unbounded — an empty box is "no limit", not zero.
    minSqft: null, maxSqft: null, minPrice: null, maxPrice: null,
    minLease: 0, leaseMax: 99,
    // Empty set means "all models" — the same "unset = no bound" rule the
    // numeric filters use, rather than pre-selecting everything.
    models: new Set(), allModels: [],
    selectedId: null, markers: new Map(), chart: null,
    playing: false, timer: null,
    schools: [], showSchools: false, selectedSchool: null,
    compareMode: false, compare: [], cmpChart: null,
    // Shared by the panel chart and the compare chart — they are mutually
    // exclusive views, so one preference rather than two that can disagree.
    measure: "psf",
    // id → slot 0-2. Held separately from `compare` (which is display order)
    // so a property keeps its colour and key when the order changes or
    // another property is removed — colour follows the entity, not its rank.
    slotOf: new Map(),
  };

  // Three is the cap: beyond that the columns stop being readable side by side
  // and the chart stops being a comparison.
  const COMPARE_MAX = 3;

  // Categorical slots 1-3, validated all-pairs in both modes. Position in this
  // list is fixed to selection order, so a colour never changes meaning when
  // another property is removed. Numbers on the markers carry the same
  // identity, so it is never colour alone.
  const CMP_COLOURS = ["--cmp-1", "--cmp-2", "--cmp-3"];

  // Primary 1 registration priority is distance-banded: inside 1 km, then
  // 1–2 km, then beyond. Both rings are drawn because the second band is a
  // real tier, not decoration.
  const P1_BANDS = [1000, 2000];
  const EARTH_R = 6371000;

  // Anchored to the newest month in the data, not to today: the datasets lag
  // reality by weeks, so "1Y" from today would clip the most recent month.
  // `years` follows the same-date-last-year convention used by price charts.
  const PERIOD_PRESETS = [
    { id: "ytd", label: "YTD" },
    { id: "1y", label: "1Y", years: 1 },
    { id: "2y", label: "2Y", years: 2 },
    { id: "3y", label: "3Y", years: 3 },
    { id: "5y", label: "5Y", years: 5 },
    { id: "7y", label: "7Y", years: 7 },
    { id: "10y", label: "10Y", years: 10 },
    { id: "all", label: "All" },
  ];

  // ── formatting ────────────────────────────────────────────────────────

  const sgd = new Intl.NumberFormat("en-SG", {
    style: "currency", currency: "SGD", maximumFractionDigits: 0,
  });
  const num = new Intl.NumberFormat("en-SG");

  const money = (v) => (v == null ? "—" : sgd.format(v));
  const psfText = (v) => (v == null ? "—" : "$" + num.format(Math.round(v)));

  function monthLabel(iso) {
    const [y, m] = iso.split("-");
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][+m - 1] + " " + y;
  }

  const shortMonth = (iso) => {
    const [y, m] = iso.split("-");
    return ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][+m - 1] + " " + y.slice(2);
  };

  function median(values) {
    if (!values.length) return null;
    const s = [...values].sort((a, b) => a - b);
    const mid = s.length >> 1;
    return s.length % 2 ? s[mid] : (s[mid - 1] + s[mid]) / 2;
  }

  const cssVar = (name) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  const MS_PER_YEAR = 365.2425 * 24 * 3600 * 1000;

  /** [[YYYY-MM-01, median psf], …] in date order — the basis for the chart,
   *  the sparkline and the growth rate, so all three tell the same story. */
  function monthlyMedians(txns) {
    const byMonth = new Map();
    for (const t of txns) {
      if (t.psf == null) continue;
      if (!byMonth.has(t.date)) byMonth.set(t.date, []);
      byMonth.get(t.date).push(t.psf);
    }
    return [...byMonth.keys()].sort().map((m) => [m, median(byMonth.get(m))]);
  }

  /** Compound annual growth in psf across the selected period.
   *
   *  Measured between the first and last *monthly median* rather than the
   *  first and last individual caveat, so one high-floor outlier at either end
   *  can't set the headline rate. Under a year there is no annual rate to
   *  report, so the plain change is returned instead. */
  function growth(txns) {
    const series = monthlyMedians(txns);
    if (series.length < 2) return null;

    const [firstDate, from] = series[0];
    const [lastDate, to] = series[series.length - 1];
    if (!from || !to || from <= 0) return null;

    const years = (Date.parse(lastDate) - Date.parse(firstDate)) / MS_PER_YEAR;
    if (years <= 0) return null;

    return {
      years,
      from,
      to,
      total: to / from - 1,
      annual: years >= 1 ? Math.pow(to / from, 1 / years) - 1 : null,
      firstDate,
      lastDate,
    };
  }

  function pct(v) {
    if (v == null) return "—";
    const s = (v * 100).toFixed(1);
    return (v > 0 ? "+" : "") + s + "%";
  }

  /** Lease position as of today, so the countdown stays right between runs. */
  function lease(prop) {
    const label = prop.tenure_label || "";
    if (!prop.lease_start || !prop.lease_years) {
      return { label, yearsLeft: null, expiry: null };
    }
    const expiry = prop.lease_start + prop.lease_years;
    const now = new Date();
    const elapsed = (now - Date.parse(`${prop.lease_start}-01-01`)) / MS_PER_YEAR;
    return { label, expiry, yearsLeft: Math.max(0, prop.lease_years - elapsed) };
  }

  function sparkline(series, w = 172, h = 38) {
    if (series.length < 2) return "";
    const vals = series.map((s) => s[1]);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const span = hi - lo || 1;
    const pts = series.map((s, i) => {
      const x = 1 + (i / (series.length - 1)) * (w - 2);
      const y = h - 2 - ((s[1] - lo) / span) * (h - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    const [lx, ly] = pts[pts.length - 1].split(",");
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}"
              preserveAspectRatio="none" aria-hidden="true">
      <polyline points="${pts.join(" ")}" fill="none" stroke="currentColor"
                stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
      <circle cx="${lx}" cy="${ly}" r="2.6" fill="currentColor"/>
    </svg>`;
  }

  // ── colour scale ──────────────────────────────────────────────────────
  // Computed once from the whole dataset and then held fixed: moving the time
  // slider must change a marker's value, never the meaning of a colour.

  function buildThresholds(allPsf) {
    if (!allPsf.length) return [];
    const min = Math.min(...allPsf), max = Math.max(...allPsf);
    if (min === max) return [];
    const width = (max - min) / PSF_STEPS;
    const round = width > 400 ? 100 : width > 150 ? 50 : width > 40 ? 10 : 5;
    const out = [];
    for (let i = 1; i < PSF_STEPS; i++) {
      out.push(Math.round((min + width * i) / round) * round);
    }
    return [...new Set(out)];
  }

  function psfColour(psf) {
    if (psf == null) return cssVar("--psf-none");
    let bin = 0;
    while (bin < state.thresholds.length && psf >= state.thresholds[bin]) bin++;
    return cssVar(`--psf-${Math.min(bin + 1, PSF_STEPS)}`);
  }

  function renderLegend() {
    const t = state.thresholds;
    const bands = t.length ? t.length + 1 : 1;
    let html = "";
    for (let i = 0; i < bands; i++) {
      const tick = i === 0 ? "" : psfText(t[i - 1]);
      html += `<div class="legend-step">
                 <div class="legend-swatch" style="background:var(--psf-${i + 1})"></div>
                 <div class="legend-tick">${tick}</div>
               </div>`;
    }
    el.legendScale.innerHTML = html;
    el.legend.hidden = false;
  }

  // ── time filtering ────────────────────────────────────────────────────

  const rangeBounds = () => [state.months[state.startIdx], state.months[state.endIdx]];

  /** Transactions surviving every transaction-level filter: period, size and
   *  price. A property whose transactions are all excluded drops off the map,
   *  which is what makes "5-room over 1,200 sqft under $1.2M" answerable. */
  function matchingTxns(prop) {
    const [from, to] = rangeBounds();
    const { minSqft, maxSqft, minPrice, maxPrice } = state;

    return prop.txns.filter((t) => {
      if (t.date < from || t.date > to) return false;
      if (minPrice != null && !(t.price >= minPrice)) return false;
      if (maxPrice != null && !(t.price <= maxPrice)) return false;
      if (minSqft != null || maxSqft != null) {
        // A row with no area can't satisfy a size bound; excluding it beats
        // silently passing it through as if it had.
        if (t.area_sqft == null) return false;
        if (minSqft != null && t.area_sqft < minSqft) return false;
        if (maxSqft != null && t.area_sqft > maxSqft) return false;
      }
      return true;
    });
  }

  function medianPsf(txns) {
    return median(txns.map((t) => t.psf).filter((p) => p != null));
  }

  /** Years remaining on the lease, or null when the question doesn't apply
   *  (freehold) or the data doesn't say. */
  function yearsLeft(prop) {
    return lease(prop).yearsLeft;
  }

  /** Property-level filters. Lease is a fact about the building, not about any
   *  one transaction, so it hides the property outright. Freehold and
   *  unknown-lease properties pass any minimum — a freehold outlasts every
   *  threshold, and hiding what we can't assess would quietly lose data. */
  const modelOf = (p) => p.model || p.flat_model || p.type || "";

  function visibleProperties() {
    return state.properties.filter((p) => {
      if (state.source !== "ALL" && p.source !== state.source) return false;
      if (state.models.size && !state.models.has(modelOf(p))) return false;
      if (state.minLease > 0) {
        const left = yearsLeft(p);
        if (left != null && left < state.minLease) return false;
      }
      return true;
    });
  }

  function activeFilterCount() {
    let n = 0;
    if (state.source !== "ALL") n++;
    if (state.minSqft != null) n++;
    if (state.maxSqft != null) n++;
    if (state.minPrice != null) n++;
    if (state.maxPrice != null) n++;
    if (state.minLease > 0) n++;
    if (state.models.size) n++;
    if (state.startIdx > 0 || state.endIdx < state.months.length - 1) n++;
    return n;
  }

  /** Great-circle distance in metres. Straight-line "as the crow flies" is
   *  exactly what MOE uses for P1 — not walking or driving distance. */
  function distanceM(aLat, aLng, bLat, bLng) {
    const toRad = Math.PI / 180;
    const dLat = (bLat - aLat) * toRad;
    const dLng = (bLng - aLng) * toRad;
    const s =
      Math.sin(dLat / 2) ** 2 +
      Math.cos(aLat * toRad) * Math.cos(bLat * toRad) * Math.sin(dLng / 2) ** 2;
    return 2 * EARTH_R * Math.asin(Math.sqrt(s));
  }

  // ── map ───────────────────────────────────────────────────────────────

  let map, layer;

  function initMap() {
    map = L.map(el.map, { zoomControl: true, attributionControl: true })
      .setView(SG_CENTRE, 12);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    // Rings under the school markers, both under the property markers.
    ringLayer = L.layerGroup().addTo(map);
    schoolLayer = L.layerGroup().addTo(map);
    layer = L.layerGroup().addTo(map);
    map.on("zoomend moveend", updateLabels);
    map.on("movestart zoomstart", hideHoverCard);   // never leave it stranded
  }

  // Leaflet solves fitBounds against the container's pixel size, so a fit
  // issued before first layout resolves to zoom 0 — the whole world. That is
  // the normal case here, not an edge case: data.json is local and resolves
  // long before the render-blocking Leaflet stylesheet arrives from the CDN,
  // so at that moment the map has no size at all. Wait for a real one.
  let sizeObserver = null;

  function whenMapSized(fn) {
    sizeObserver?.disconnect();
    let lastH = -1;

    // Not merely "non-zero" — the header is still settling as chips wrap and
    // fonts land, so the first non-zero height is a transient one and fitting
    // against it lands a zoom level or two too far out. Wait for two frames at
    // the same height.
    const check = () => {
      const h = el.map.clientHeight;
      if (h > 0 && h === lastH) {
        sizeObserver?.disconnect();
        sizeObserver = null;
        fn();
        return;
      }
      lastH = h;
      requestAnimationFrame(check);
    };

    sizeObserver = new ResizeObserver(() => { lastH = -1; });
    sizeObserver.observe(el.map);
    requestAnimationFrame(check);
  }

  const CAN_HOVER = matchMedia("(hover: hover) and (pointer: fine)").matches;
  const HOVER_GAP = 15;

  /** Sits above the marker, flipping below when there isn't room and clamping
   *  to the stage horizontally, so it is never clipped at an edge. */
  function showHoverCard(prop, txns, psf, marker) {
    const node = el.hoverCard;
    if (!node) return;        // cached older index.html — degrade, don't break
    node.innerHTML = hoverCard(prop, txns, psf);
    node.hidden = false;

    const pt = map.latLngToContainerPoint(marker.getLatLng());
    const stage = el.stage.getBoundingClientRect();
    const card = node.getBoundingClientRect();

    let top = pt.y - card.height - HOVER_GAP;
    if (top < 6) top = pt.y + HOVER_GAP + 12;             // flip below
    let left = pt.x - card.width / 2;
    left = Math.max(6, Math.min(left, stage.width - card.width - 6));

    node.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  }

  function hideHoverCard() {
    if (el.hoverCard) el.hoverCard.hidden = true;
  }

  /** The hover summary: the same numbers as the panel, minus the interaction.
   *  Built as a string because Leaflet tooltips take HTML, and rebuilt on each
   *  render so it always reflects the current period. */
  function hoverCard(prop, txns, psf) {
    const g = growth(txns);
    const prices = txns.map((t) => t.price).filter(Boolean);
    const series = monthlyMedians(txns);
    const shape = prop.source === "HDB" ? "mk--hdb" : "mk--ura";

    const growthLine = g
      ? `<span class="hc-growth is-${g.annual > 0 || g.total > 0 ? "up" : "down"}">
           ${pct(g.annual != null ? g.annual : g.total)}
           <span class="hc-growth-unit">${g.annual != null ? "p.a." : "total"}</span>
         </span>`
      : `<span class="hc-growth is-flat">—</span>`;

    return `<div class="hover-card">
      <div class="hc-head">
        <i class="mk ${shape}" style="background:${psfColour(psf)}"></i>
        <span class="hc-name">${escapeHtml(prop.name)}</span>
      </div>

      <div class="hc-hero">
        <span class="hc-psf">${psfText(psf)}</span>
        <span class="hc-psf-unit">psf median</span>
        ${growthLine}
      </div>

      ${series.length > 1
        ? `<div class="hc-spark">${sparkline(series)}</div>`
        : `<div class="hc-spark hc-spark--empty">single month of data</div>`}

      <dl class="hc-stats">
        <div><dt>Transactions</dt><dd>${num.format(txns.length)}</dd></div>
        <div><dt>Median price</dt><dd>${prices.length ? compactMoney(median(prices)) : "—"}</dd></div>
        <div><dt>Latest</dt><dd>${txns.length ? monthLabel(txns[txns.length - 1].date) : "—"}</dd></div>
      </dl>

      <p class="hc-hint">Click for full history</p>
    </div>`;
  }

  function markerIcon(prop, psf, showLabel) {
    const shape = prop.source === "HDB" ? "mk--hdb" : "mk--ura";
    const label = showLabel && psf != null
      ? `<span class="mk-label">${psfText(psf)}</span>` : "";
    // A compared marker carries its slot number as well as the slot colour, so
    // the pairing with the drawer never depends on colour alone.
    const compared = comparedIndex(prop.id) !== -1;
    const slot = compared ? slotOf(prop.id) : -1;
    const badge = !compared ? ""
      : `<span class="mk-badge" style="background:var(${CMP_COLOURS[slot]})"
             >${comparedIndex(prop.id) + 1}</span>`;
    return L.divIcon({
      className: "mk-wrap" + (prop.id === state.selectedId ? " is-selected" : "")
        + (compared ? " is-compared" : ""),
      html: `<span class="mk ${shape}" style="background:${psfColour(psf)}"></span>${badge}${label}`,
      iconSize: [24, 24],
      iconAnchor: [12, 12],
    });
  }

  function renderMarkers({ fit = false } = {}) {
    layer.clearLayers();
    state.markers.clear();

    const props = visibleProperties();
    const withCoords = props.filter((p) => p.lat != null && p.lng != null);
    const points = [];

    for (const prop of withCoords) {
      const txns = matchingTxns(prop);
      if (!txns.length) continue;                  // nothing transacted in range
      const psf = medianPsf(txns);

      const marker = L.marker([prop.lat, prop.lng], {
        icon: markerIcon(prop, psf, true),
        keyboard: true,
        title: `${prop.name} — ${psfText(psf)} psf`,
        riseOnHover: true,
      });
      marker.on("click", () =>
        state.compareMode ? addCompare(prop.id) : selectProperty(prop.id));
      // Hover only where hovering exists — on touch, a tap opens the full
      // panel and a hover card would just flash over it.
      if (CAN_HOVER) {
        marker.on("mouseover", () => showHoverCard(prop, txns, psf, marker));
        marker.on("mouseout", hideHoverCard);
      }
      marker.addTo(layer);
      state.markers.set(prop.id, marker);
      points.push([prop.lat, prop.lng]);
    }

    if (fit && points.length) {
      // Leaflet caches the container size at init; the top bar's height is not
      // final until its filter row has wrapped, and a stale (or zero) height
      // makes fitBounds solve for the minimum zoom — the whole world.
      const bounds = L.latLngBounds(points).pad(0.3);
      whenMapSized(() => {
        map.invalidateSize({ animate: false });
        map.fitBounds(bounds, { maxZoom: 17 });
      });
    }
    updateLabels();
    updateMeta(props, withCoords.length, points.length);
  }

  // Blocks on the same street sit metres apart, so at most zoom levels their
  // value labels would sit on top of each other. Keep a label only where it
  // doesn't collide with one already placed; the rest read via hover or click.
  function updateLabels() {
    const placed = [];
    for (const marker of state.markers.values()) {
      const node = marker.getElement();
      if (!node) continue;
      const pt = map.latLngToContainerPoint(marker.getLatLng());
      const clash = placed.some(
        (p) => Math.abs(p.x - pt.x) < 78 && Math.abs(p.y - pt.y) < 24);
      node.classList.toggle("no-label", clash);
      if (!clash) placed.push(pt);
    }
  }

  function updateMeta(props, geocoded, shown) {
    const [from, to] = rangeBounds();
    const total = props.reduce((n, p) => n + matchingTxns(p).length, 0);
    const missing = props.length - geocoded;
    const bits = [
      `${num.format(total)} transactions`,
      `${shown} of ${state.properties.length} mapped`,
      `${monthLabel(from)} – ${monthLabel(to)}`,
    ];
    if (missing) bits.push(`${missing} without coordinates`);
    if (state.generatedAt) bits.push(`updated ${state.generatedAt}`);
    el.meta.textContent = bits.join(" · ");

    // Say which filter emptied the map, rather than showing a blank one.
    if (shown === 0 && state.properties.length) {
      el.emptyNote.textContent = props.length === 0
        ? "No properties match these filters. Try clearing the type or model, "
          + "or lowering the lease minimum."
        : "No transactions match these filters. Try widening the size, price or period.";
      el.emptyNote.hidden = false;
    } else {
      el.emptyNote.hidden = true;
    }
  }

  // ── compare ───────────────────────────────────────────────────────────

  const comparedIndex = (id) => state.compare.indexOf(id);

  function toggleCompare() {
    state.compareMode = !state.compareMode;
    el.compareToggle.classList.toggle("is-on", state.compareMode);
    el.compareToggle.setAttribute("aria-pressed", String(state.compareMode));
    el.compareBar.hidden = !state.compareMode;

    if (state.compareMode) {
      closePanel();                       // one detail view at a time
      el.compareSearch.focus();
    } else {
      hideResults();
    }
    renderCompare();
    renderMarkers();
    resizeMap();
  }

  /** The colour a property owns, stable for as long as it stays selected —
   *  so removing one never repaints the others.
   *
   *  The *number* is deliberately not this: it follows position, so the
   *  leftmost card is always #1. Number answers "where is it", colour answers
   *  "which is it", and both update together on the map badge. */
  const slotOf = (id) => state.slotOf.get(id) ?? 0;

  function addCompare(id) {
    if (comparedIndex(id) !== -1) return removeCompare(id);   // click again to drop
    if (state.compare.length >= COMPARE_MAX) {
      flashHint(`${COMPARE_MAX} of ${COMPARE_MAX} — remove one first`);
      return;
    }
    // Lowest free slot, so removing #1 and adding another reuses blue rather
    // than shuffling everyone else's colour along.
    const taken = new Set(state.slotOf.values());
    let slot = 0;
    while (taken.has(slot) && slot < COMPARE_MAX) slot++;
    state.slotOf.set(id, slot);
    state.compare.push(id);

    if (!state.compareMode) toggleCompare();
    else refreshCompare();
  }

  function removeCompare(id) {
    state.compare = state.compare.filter((x) => x !== id);
    state.slotOf.delete(id);
    refreshCompare();
  }

  function clearCompare() {
    state.compare = [];
    state.slotOf.clear();
    refreshCompare();
  }

  /** Move a property one place left or right in the display order. Only the
   *  order changes — each property keeps its slot colour and key, so the
   *  chart and the map markers don't recolour underneath the reader. */
  function moveCompare(id, delta) {
    const from = comparedIndex(id);
    const to = from + delta;
    if (from === -1 || to < 0 || to >= state.compare.length) return;
    const next = [...state.compare];
    [next[from], next[to]] = [next[to], next[from]];
    state.compare = next;
    refreshCompare();

    // Keep focus on the button the user just pressed, so a second nudge
    // doesn't need the mouse again.
    requestAnimationFrame(() => {
      const btn = el.compareSlots.querySelector(
        `[data-move="${id}"][data-dir="${delta}"]`);
      if (btn && !btn.disabled) btn.focus();
    });
  }

  /** Move a property to an absolute position — what a drop needs. */
  function moveCompareTo(id, index) {
    const from = comparedIndex(id);
    if (from === -1 || index < 0 || index >= state.compare.length || from === index) return;
    const next = [...state.compare];
    next.splice(index, 0, ...next.splice(from, 1));
    state.compare = next;
    refreshCompare();
  }

  /** HTML5 drag on the columns. The arrow buttons stay: drag is unavailable
   *  on touch and awkward by keyboard, so it's the shortcut, not the only way. */
  function wireColumnDrag() {
    let draggingId = null;

    for (const col of el.compareBody.querySelectorAll(".cmp-col")) {
      col.addEventListener("dragstart", (e) => {
        draggingId = col.dataset.id;
        col.classList.add("is-dragging");
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", draggingId);   // Firefox needs data set
      });

      col.addEventListener("dragend", () => {
        draggingId = null;
        for (const c of el.compareBody.querySelectorAll(".cmp-col")) {
          c.classList.remove("is-dragging", "is-drop-target");
        }
      });

      col.addEventListener("dragover", (e) => {
        if (!draggingId || col.dataset.id === draggingId) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        col.classList.add("is-drop-target");
      });

      col.addEventListener("dragleave", () => col.classList.remove("is-drop-target"));

      col.addEventListener("drop", (e) => {
        e.preventDefault();
        const id = draggingId || e.dataTransfer.getData("text/plain");
        col.classList.remove("is-drop-target");
        if (id) moveCompareTo(id, [...el.compareBody.children].indexOf(col));
      });
    }
  }

  function refreshCompare() {
    renderCompare();
    renderMarkers();
    resizeMap();
  }

  let hintTimer = null;
  function flashHint(message) {
    el.compareHint.textContent = message;
    el.compareHint.classList.add("is-warn");
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => {
      el.compareHint.classList.remove("is-warn");
      updateCompareHint();
    }, 2200);
  }

  function updateCompareHint() {
    el.compareHint.textContent = `${state.compare.length} of ${COMPARE_MAX}`;
  }

  /** The drawer changes the map's height, so Leaflet has to be told. */
  function resizeMap() {
    if (!map) return;
    requestAnimationFrame(() => {
      map.invalidateSize({ animate: false });
      updateLabels();
    });
  }

  const comparedProps = () =>
    state.compare.map((id) => state.properties.find((p) => p.id === id)).filter(Boolean);

  function renderCompare() {
    updateCompareHint();
    renderCompareSlots();

    const n = state.compare.length;
    el.compareCount.textContent = String(n);
    el.compareCount.hidden = n === 0;
    el.comparePanel.hidden = !(state.compareMode && n > 0);

    if (el.comparePanel.hidden) {
      if (state.cmpChart) { state.cmpChart.destroy(); state.cmpChart = null; }
      return;
    }

    const [from, to] = rangeBounds();
    const active = activeFilterCount();
    el.cmpScope.textContent =
      ` · ${monthLabel(from)} – ${monthLabel(to)}` +
      (active ? ` · ${active} filter${active === 1 ? "" : "s"} applied` : "");

    el.compareBody.innerHTML = comparedProps()
      .map((prop, i) => compareColumn(prop, i)).join("");
    for (const btn of el.compareBody.querySelectorAll("[data-drop]")) {
      btn.addEventListener("click", () => removeCompare(btn.dataset.drop));
    }
    for (const btn of el.compareBody.querySelectorAll("[data-move]")) {
      btn.addEventListener("click", () => moveCompare(btn.dataset.move, +btn.dataset.dir));
    }
    wireColumnDrag();
    drawCompareChart();
  }

  function compareColumn(prop, i) {
    const s = slotOf(prop.id);
    const last = state.compare.length - 1;
    const moves = `
      <button type="button" class="cmp-move" data-move="${prop.id}" data-dir="-1"
              ${i === 0 ? "disabled" : ""} title="Move left"
              aria-label="Move ${escapeHtml(prop.name)} left">‹</button>
      <button type="button" class="cmp-move" data-move="${prop.id}" data-dir="1"
              ${i === last ? "disabled" : ""} title="Move right"
              aria-label="Move ${escapeHtml(prop.name)} right">›</button>`;
    const txns = matchingTxns(prop);
    const g = growth(txns);
    const l = lease(prop);
    const prices = txns.map((t) => t.price).filter(Boolean);
    const shape = prop.source === "HDB" ? "mk--hdb" : "mk--ura";

    // A compared property filtered out of view keeps its slot and says so —
    // dropping it would silently undo the user's selection mid-adjustment.
    if (!txns.length) {
      return `<article class="cmp-col" draggable="true" data-id="${prop.id}"
               style="--slot:var(${CMP_COLOURS[s]})">
        <header class="cmp-col-head">
          <span class="cmp-key">${i + 1}</span>
          <span class="cmp-name">${escapeHtml(prop.name)}</span>
          <span class="cmp-moves">${moves}</span>
          <button type="button" class="cmp-drop" data-drop="${prop.id}"
                  aria-label="Remove ${escapeHtml(prop.name)}">&times;</button>
        </header>
        <p class="cmp-empty">No transactions match the current filters.</p>
      </article>`;
    }

    const rows = [
      ["Median psf", psfText(medianPsf(txns))],
      ["Growth", g ? `${pct(g.annual != null ? g.annual : g.total)}` +
        `<span class="cmp-sub">${g.annual != null ? "per year" : "over period"}</span>` : "—"],
      ["Transactions", num.format(txns.length)],
      ["Median price", prices.length ? compactMoney(median(prices)) : "—"],
      ["Latest", monthLabel(txns[txns.length - 1].date)],
      ["Type", escapeHtml([prop.type, prop.model].filter(Boolean)
        .filter((v, k, a) => a.indexOf(v) === k).join(" · "))],
      ["Tenure", escapeHtml(l.label || "—")],
      [prop.source === "HDB" ? "Completed" : "TOP", prop.top_year || "—"],
      ["Lease left", l.yearsLeft != null
        ? `${Math.floor(l.yearsLeft)} yrs<span class="cmp-sub">to ${l.expiry}</span>` : "—"],
    ];

    return `<article class="cmp-col" draggable="true" data-id="${prop.id}"
             style="--slot:var(${CMP_COLOURS[s]})">
      <header class="cmp-col-head">
        <span class="cmp-key">${i + 1}</span>
        <i class="mk ${shape}" style="background:var(${CMP_COLOURS[s]})" aria-hidden="true"></i>
        <span class="cmp-name" title="${escapeHtml(prop.name)}">${escapeHtml(prop.name)}</span>
        <span class="cmp-moves">${moves}</span>
        <button type="button" class="cmp-drop" data-drop="${prop.id}"
                aria-label="Remove ${escapeHtml(prop.name)}">&times;</button>
      </header>
      <p class="cmp-addr">${escapeHtml(prop.address || prop.district_town || "")}</p>
      <dl class="cmp-rows">${rows.map(([k, v]) =>
        `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
    </article>`;
  }

  function renderCompareSlots() {
    if (!state.compare.length) {
      el.compareSlots.innerHTML =
        `<span class="slots-empty">Click a marker, or search above</span>`;
      return;
    }
    const last = state.compare.length - 1;
    el.compareSlots.innerHTML = comparedProps().map((prop, i) => {
      const s = slotOf(prop.id);
      return `<span class="slot" style="--slot:var(${CMP_COLOURS[s]})">
        <button type="button" class="slot-move" data-move="${prop.id}" data-dir="-1"
                ${i === 0 ? "disabled" : ""}
                aria-label="Move ${escapeHtml(prop.name)} left"
                title="Move left">‹</button>
        <span class="cmp-key">${i + 1}</span>${escapeHtml(prop.name)}
        <button type="button" class="slot-move" data-move="${prop.id}" data-dir="1"
                ${i === last ? "disabled" : ""}
                aria-label="Move ${escapeHtml(prop.name)} right"
                title="Move right">›</button>
        <button type="button" class="slot-x" data-drop="${prop.id}"
                aria-label="Remove ${escapeHtml(prop.name)}">&times;</button>
      </span>`;
    }).join("");

    for (const btn of el.compareSlots.querySelectorAll("[data-drop]")) {
      btn.addEventListener("click", () => removeCompare(btn.dataset.drop));
    }
    for (const btn of el.compareSlots.querySelectorAll("[data-move]")) {
      btn.addEventListener("click", () => moveCompare(btn.dataset.move, +btn.dataset.dir));
    }
  }

  /** One chart, up to three series — overlaying them is the whole point;
   *  three separate charts would leave the reader comparing axes. */
  function drawCompareChart() {
    const canvas = $("cmpChart");
    if (!canvas) return;
    if (state.cmpChart) { state.cmpChart.destroy(); state.cmpChart = null; }

    const series = comparedProps().map((prop) => ({
      prop, slot: slotOf(prop.id),
      points: new Map(monthlyMedians(matchingTxns(prop))),
    }));
    const months = [...new Set(series.flatMap((s) => [...s.points.keys()]))].sort();
    if (!months.length) return;

    // In growth mode each line is rebased to its own first month *inside the
    // selected period*, so properties at very different price levels can be
    // compared on one axis — which is the point of the mode. Indexing to a
    // common base is also the only honest way to put them on one scale; two
    // y-axes would invent a correlation that isn't in the data.
    const growthMode = state.measure === "growth";
    for (const s of series) {
      const firstMonth = months.find((m) => s.points.has(m));
      s.base = firstMonth != null ? s.points.get(firstMonth) : null;
      s.baseMonth = firstMonth;
    }

    const valueAt = (s, m) => {
      if (!s.points.has(m)) return null;
      const v = s.points.get(m);
      if (!growthMode) return Math.round(v);
      if (!s.base) return null;
      return +(((v / s.base) - 1) * 100).toFixed(1);
    };

    // Bases can differ when one property has no sale in the opening months —
    // say so rather than let the reader assume a shared starting line.
    const bases = [...new Set(series.map((s) => s.baseMonth).filter(Boolean))];
    el.cmpChartTitle.textContent = growthMode
      ? "Growth in price per sqft" : "Price per sqft over time";
    el.cmpChartSub.textContent = growthMode
      ? (bases.length === 1
          ? `Change since ${monthLabel(bases[0])}, each line from its own first month`
          : "Change since each property's first month in the selected period")
      : "Monthly median, one line per property";

    const grid = cssVar("--gridline");
    const muted = cssVar("--text-muted");
    const surface = cssVar("--surface-1");

    state.cmpChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: months,
        datasets: series.map((s) => {
          const colour = cssVar(CMP_COLOURS[s.slot]);
          return {
            label: s.prop.name,
            data: months.map((m) => valueAt(s, m)),
            borderColor: colour,
            backgroundColor: colour,
            borderWidth: 2,
            tension: 0.25,
            spanGaps: true,          // months with no sale are gaps, not zeroes
            pointRadius: months.length > 40 ? 0 : 3,
            pointHoverRadius: 7,
            pointHitRadius: 14,
            pointBackgroundColor: surface,
            pointBorderColor: colour,
            pointBorderWidth: 2,
          };
        }),
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: true,
            position: "bottom",
            labels: {
              color: cssVar("--text-secondary"),
              boxWidth: 10, boxHeight: 10, usePointStyle: true,
              pointStyle: "circle", font: { size: 11 },
            },
          },
          tooltip: {
            backgroundColor: surface,
            titleColor: cssVar("--text-primary"),
            bodyColor: cssVar("--text-secondary"),
            borderColor: grid,
            borderWidth: 1,
            padding: 9,
            callbacks: {
              title: (items) => monthLabel(items[0].label),
              label: (item) => {
                if (!growthMode) return `${item.dataset.label}: ${psfText(item.parsed.y)}`;
                const s = series[item.datasetIndex];
                const raw = s.points.get(months[item.dataIndex]);
                return `${item.dataset.label}: ${pct(item.parsed.y / 100)}`
                  + (raw ? ` (${psfText(raw)})` : "");
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: grid },
            ticks: {
              color: muted, font: { size: 10 }, maxRotation: 0, autoSkipPadding: 20,
              callback(v, i) { return shortMonth(this.getLabelForValue(i)); },
            },
          },
          y: {
            grid: {
              drawTicks: false,
              // The baseline is the reference the whole mode hangs on, so it
              // gets a stronger hairline than the rest of the grid.
              color: (ctx) => (growthMode && ctx.tick.value === 0
                ? cssVar("--baseline") : grid),
            },
            border: { display: false },
            ticks: {
              color: muted, font: { size: 10 }, padding: 6, maxTicksLimit: 5,
              callback: (v) => (growthMode
                ? (v > 0 ? "+" : "") + v + "%"
                : "$" + num.format(v)),
            },
          },
        },
      },
    });
  }

  /** Both chart toggles set the same preference and re-sync whichever chip
   *  group is currently on screen. */
  function setMeasure(measure) {
    state.measure = measure;
    for (const group of [el.cmpModeChips, document.getElementById("panelModeChips")]) {
      if (!group) continue;
      for (const chip of group.querySelectorAll(".chip")) {
        const on = chip.dataset.cmpmode === measure;
        chip.classList.toggle("is-on", on);
        chip.setAttribute("aria-pressed", String(on));
      }
    }
    // renderPanel, not drawChart: the heading and its chips are part of the
    // panel template, so redrawing only the canvas leaves the title lying.
    if (state.selectedId) renderPanel();
    if (!el.comparePanel.hidden) drawCompareChart();
  }

  // ── compare search ────────────────────────────────────────────────────

  let results = [];
  let highlight = -1;

  function searchProperties(query) {
    const q = query.trim().toLowerCase();
    if (!q) return [];
    // Name first, then address, so typing a condo name doesn't bury it under
    // every block on the same street.
    const byName = [], byAddress = [];
    for (const p of state.properties) {
      if (comparedIndex(p.id) !== -1) continue;            // already chosen
      if (p.name.toLowerCase().includes(q)) byName.push(p);
      else if ((p.address || "").toLowerCase().includes(q)) byAddress.push(p);
    }
    return [...byName, ...byAddress].slice(0, 8);
  }

  function onSearchInput() {
    results = searchProperties(el.compareSearch.value);
    highlight = results.length ? 0 : -1;
    renderResults();
  }

  function renderResults() {
    if (!results.length) return hideResults();
    el.compareResults.innerHTML = results.map((p, i) => `
      <li role="option" id="cmp-opt-${i}" data-id="${p.id}"
          class="combo-item${i === highlight ? " is-active" : ""}"
          aria-selected="${i === highlight}">
        <i class="mk ${p.source === "HDB" ? "mk--hdb" : "mk--ura"}"
           style="background:${psfColour(medianPsf(matchingTxns(p)))}" aria-hidden="true"></i>
        <span class="combo-name">${escapeHtml(p.name)}</span>
        <span class="combo-meta">${escapeHtml([p.model, p.district_town]
          .filter(Boolean).join(" · "))}</span>
      </li>`).join("");
    el.compareResults.hidden = false;
    el.compareSearch.setAttribute("aria-expanded", "true");
    el.compareSearch.setAttribute("aria-activedescendant",
      highlight >= 0 ? `cmp-opt-${highlight}` : "");

    for (const li of el.compareResults.querySelectorAll(".combo-item")) {
      li.addEventListener("mousedown", (e) => {   // before blur closes the list
        e.preventDefault();
        pickResult(li.dataset.id);
      });
    }
  }

  function hideResults() {
    el.compareResults.hidden = true;
    el.compareResults.innerHTML = "";
    el.compareSearch.setAttribute("aria-expanded", "false");
    el.compareSearch.removeAttribute("aria-activedescendant");
    results = [];
    highlight = -1;
  }

  function pickResult(id) {
    addCompare(id);
    el.compareSearch.value = "";
    hideResults();
    const prop = state.properties.find((p) => p.id === id);
    if (prop && prop.lat != null) map.panTo([prop.lat, prop.lng], { animate: true });
  }

  function onSearchKey(e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!results.length) return;
      e.preventDefault();
      highlight = (highlight + (e.key === "ArrowDown" ? 1 : -1) + results.length)
        % results.length;
      renderResults();
    } else if (e.key === "Enter") {
      if (highlight >= 0 && results[highlight]) {
        e.preventDefault();
        pickResult(results[highlight].id);
      }
    } else if (e.key === "Escape") {
      hideResults();
    }
  }

  // ── schools & P1 distance rings ───────────────────────────────────────

  let schoolLayer, ringLayer;

  function toggleSchools() {
    state.showSchools = !state.showSchools;
    el.schoolsToggle.classList.toggle("is-on", state.showSchools);
    el.schoolsToggle.setAttribute("aria-pressed", String(state.showSchools));
    if (el.legendSchool) el.legendSchool.hidden = !state.showSchools;
    if (!state.showSchools) clearSchoolSelection();
    renderSchools();
  }

  function renderSchools() {
    schoolLayer.clearLayers();
    if (!state.showSchools) return;

    for (const school of state.schools) {
      if (school.lat == null || school.lng == null) continue;
      const selected = state.selectedSchool && state.selectedSchool.postal === school.postal;
      const marker = L.marker([school.lat, school.lng], {
        icon: L.divIcon({
          className: "sk-wrap" + (selected ? " is-selected" : ""),
          html: `<span class="sk" title="${escapeHtml(school.name)}"></span>`,
          iconSize: [18, 18],
          iconAnchor: [9, 9],
        }),
        title: `${school.name} — click for the 1 km P1 radius`,
        // Below property markers: the properties are the subject, schools the
        // reference layer.
        zIndexOffset: -500,
      });
      marker.on("click", (e) => {
        L.DomEvent.stop(e);
        selectSchool(school);
      });
      marker.addTo(schoolLayer);
    }
  }

  function selectSchool(school) {
    // Clicking the same school again clears it, so there's a way out that
    // doesn't require finding the close button.
    if (state.selectedSchool && state.selectedSchool.postal === school.postal) {
      clearSchoolSelection();
      renderSchools();
      return;
    }
    state.selectedSchool = school;
    drawRings(school);
    renderSchools();
    renderSchoolPanel(school);
  }

  function clearSchoolSelection() {
    state.selectedSchool = null;
    ringLayer.clearLayers();
    if (!state.selectedId) closePanel();
  }

  function drawRings(school) {
    ringLayer.clearLayers();
    const centre = [school.lat, school.lng];
    // Outer first so the 1 km ring paints over it.
    for (const radius of [...P1_BANDS].reverse()) {
      L.circle(centre, {
        radius,
        className: radius === 1000 ? "ring ring--1km" : "ring ring--2km",
        interactive: false,
      }).addTo(ringLayer);
    }
    // latLng.toBounds() takes the box's full width in metres and needs no map.
    // Circle.getBounds() would be the obvious call, but it reads this._map and
    // throws on a circle that hasn't been added yet.
    const outer = P1_BANDS[P1_BANDS.length - 1];
    map.fitBounds(L.latLng(centre).toBounds(outer * 2.4), { maxZoom: 16 });
  }

  /** Which watched properties fall in each P1 band. Uses the visible set, so
   *  the other filters still apply — "5-room under $1.2M within 1 km of this
   *  school" is the question worth answering. */
  function propertiesNear(school) {
    const out = [];
    for (const prop of visibleProperties()) {
      if (prop.lat == null || prop.lng == null) continue;
      if (!matchingTxns(prop).length) continue;
      const d = distanceM(school.lat, school.lng, prop.lat, prop.lng);
      if (d <= P1_BANDS[P1_BANDS.length - 1]) out.push({ prop, d });
    }
    return out.sort((a, b) => a.d - b.d);
  }

  function renderSchoolPanel(school) {
    const near = propertiesNear(school);
    const within1 = near.filter((n) => n.d <= P1_BANDS[0]);
    const band2 = near.filter((n) => n.d > P1_BANDS[0]);

    const rows = (list) => list.map(({ prop, d }) => {
      const txns = matchingTxns(prop);
      return `<tr>
        <td>${escapeHtml(prop.name)}<span class="sp-model">${escapeHtml(prop.model || "")}</span></td>
        <td class="num">${Math.round(d)} m</td>
        <td class="num">${psfText(medianPsf(txns))}</td>
      </tr>`;
    }).join("");

    el.panelBody.innerHTML = `
      <p class="p-eyebrow"><i class="sk sk--legend" aria-hidden="true"></i>Primary school</p>
      <h2 class="p-name">${escapeHtml(school.name)}</h2>
      <p class="p-sub">${escapeHtml(school.address)} · S(${escapeHtml(school.postal)})</p>

      <div class="p-hero">
        <span class="p-hero-value">${within1.length}</span>
        <span class="p-hero-unit">within 1 km</span>
      </div>
      <p class="p-hero-label">
        Of the ${visibleProperties().length} watched propert${visibleProperties().length === 1 ? "y" : "ies"}
        currently shown${band2.length ? ` · ${band2.length} more in the 1–2 km band` : ""}
      </p>

      <h3 class="p-h3">Within 1 km</h3>
      <p class="p-h3-sub">Straight-line distance, as MOE measures it</p>
      ${within1.length ? `<div class="p-table-wrap"><table class="p-table">
        <thead><tr><th>Property</th><th class="num">Distance</th><th class="num">PSF</th></tr></thead>
        <tbody>${rows(within1)}</tbody></table></div>`
        : `<p class="p-empty">No watched properties within 1 km.</p>`}

      ${band2.length ? `<h3 class="p-h3" style="margin-top:20px">1–2 km</h3>
        <p class="p-h3-sub">Second priority band</p>
        <div class="p-table-wrap"><table class="p-table">
        <thead><tr><th>Property</th><th class="num">Distance</th><th class="num">PSF</th></tr></thead>
        <tbody>${rows(band2)}</tbody></table></div>` : ""}

      ${school.url ? `<p class="sp-link"><a href="${escapeHtml(school.url)}"
         target="_blank" rel="noopener noreferrer">School website ↗</a></p>` : ""}
      <p class="sp-note">Distances are computed from the school's registered
        postal code to each block's geocoded position, so treat them as
        indicative near the 1 km boundary — check MOE's own tool before
        relying on it.</p>
    `;
    el.panel.hidden = false;
    el.scrim.hidden = false;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
  }

  // ── panel ─────────────────────────────────────────────────────────────

  function selectProperty(id) {
    state.selectedSchool = null;      // the panel shows one thing at a time
    ringLayer.clearLayers();
    renderSchools();
    state.selectedId = id;
    renderPanel();
    renderMarkers();
    const marker = state.markers.get(id);
    if (marker) map.panTo(marker.getLatLng(), { animate: true });
  }

  function closePanel() {
    state.selectedId = null;
    el.panel.hidden = true;
    el.scrim.hidden = true;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    if (state.selectedSchool) {
      state.selectedSchool = null;
      ringLayer.clearLayers();
      renderSchools();
    }
    renderMarkers();
  }

  function renderPanel() {
    const prop = state.properties.find((p) => p.id === state.selectedId);
    if (!prop) return;

    const txns = matchingTxns(prop);
    const psf = medianPsf(txns);
    const prices = txns.map((t) => t.price).filter(Boolean);
    const shape = prop.source === "HDB" ? "mk--hdb" : "mk--ura";
    const kind = prop.source === "HDB" ? "HDB Resale" : "Private";

    // For HDB the address *is* the name, so it would just repeat the heading.
    const growthView = state.measure === "growth";
    const subtitle = [prop.type, prop.address, prop.segment]
      .filter(Boolean)
      .filter((v) => v !== prop.name)
      .filter((v, i, a) => a.indexOf(v) === i)
      .join(" · ");

    el.panelBody.innerHTML = `
      <p class="p-eyebrow"><i class="mk ${shape}" style="background:${psfColour(psf)}"></i>${kind}</p>
      <h2 class="p-name">${escapeHtml(prop.name)}</h2>
      <p class="p-sub">${escapeHtml(subtitle)}</p>

      <div class="p-hero">
        <span class="p-hero-value">${psfText(psf)}</span>
        <span class="p-hero-unit">per sqft</span>
      </div>
      <p class="p-hero-label">Median across the selected period</p>

      <div class="p-stats">
        <div class="p-stat"><div class="p-stat-k">Transactions</div>
          <div class="p-stat-v">${num.format(txns.length)}</div></div>
        <div class="p-stat"><div class="p-stat-k">Median price</div>
          <div class="p-stat-v">${prices.length ? compactMoney(median(prices)) : "—"}</div></div>
        <div class="p-stat"><div class="p-stat-k">Latest</div>
          <div class="p-stat-v">${txns.length ? monthLabel(txns[txns.length - 1].date) : "—"}</div></div>
      </div>

      ${growthHtml(growth(txns))}
      ${factsHtml(prop)}

      <div class="cmp-chart-head">
        <div class="cmp-chart-titles">
          <h3 class="p-h3" id="panelChartTitle">${growthView
            ? "Growth in price per sqft" : "Price per sqft over time"}</h3>
          <p class="p-h3-sub" id="panelChartSub">${txns.length
            ? (growthView ? "Change since the first month in the selected period"
                          : "Monthly median")
            : "Monthly median — no data in range"}</p>
        </div>
        <div class="chips" id="panelModeChips" role="group" aria-label="Chart measure">
          <button type="button" class="chip${growthView ? "" : " is-on"}"
                  data-cmpmode="psf" aria-pressed="${!growthView}">Price psf</button>
          <button type="button" class="chip${growthView ? " is-on" : ""}"
                  data-cmpmode="growth" aria-pressed="${growthView}">% growth</button>
        </div>
      </div>
      <div class="chart-box"><canvas id="psfChart"></canvas></div>

      <h3 class="p-h3">Recent transactions</h3>
      <p class="p-h3-sub">Newest first${txns.length > 12 ? ", latest 12 of " + txns.length : ""}</p>
      ${transactionsTable(txns)}
    `;

    el.panel.hidden = false;
    el.scrim.hidden = false;
    for (const chip of el.panelBody.querySelectorAll("#panelModeChips .chip")) {
      chip.addEventListener("click", () => setMeasure(chip.dataset.cmpmode));
    }
    drawChart(txns);
  }

  /** The growth headline. Under a year, an annualised rate would be an
   *  extrapolation from too little data, so the plain change is shown and
   *  labelled as such. */
  function growthHtml(g) {
    if (!g) {
      return `<div class="p-growth p-growth--empty">
        Not enough transactions in this period to measure growth.</div>`;
    }
    const annual = g.annual != null;
    const value = annual ? g.annual : g.total;
    const dir = value > 0 ? "up" : value < 0 ? "down" : "flat";
    return `<div class="p-growth is-${dir}">
      <div class="p-growth-main">
        <span class="p-growth-value">${pct(value)}</span>
        <span class="p-growth-unit">${annual ? "per year" : "over the period"}</span>
      </div>
      <p class="p-growth-note">
        ${annual ? "Compound annual growth in median psf" : "Change in median psf"} ·
        ${psfText(g.from)} → ${psfText(g.to)} ·
        ${monthLabel(g.firstDate)} – ${monthLabel(g.lastDate)}
        ${annual ? `(${g.years.toFixed(1)} yrs)` : ""}
      </p>
    </div>`;
  }

  function factsHtml(prop) {
    const l = lease(prop);
    const rows = [];

    if (l.label) rows.push(["Tenure", l.label]);
    if (prop.top_year) {
      rows.push([prop.source === "HDB" ? "Completed" : "TOP", String(prop.top_year)]);
    }
    if (prop.lease_start) rows.push(["Lease start", String(prop.lease_start)]);
    if (l.yearsLeft != null) {
      rows.push([
        "Lease remaining",
        `${Math.floor(l.yearsLeft)} years <span class="p-fact-sub">· expires ${l.expiry}</span>`,
      ]);
    }
    if (prop.flat_model) rows.push(["Flat model", prop.flat_model]);
    if (prop.district_town && prop.source === "URA") {
      rows.push(["District", `D${String(prop.district_town).padStart(2, "0")}`]);
    }
    if (!rows.length) return "";

    return `<h3 class="p-h3">Property</h3>
      <p class="p-h3-sub">Lease remaining is as of today</p>
      <dl class="p-facts">${rows.map(
        ([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
  }

  function compactMoney(v) {
    if (v == null) return "—";
    return v >= 1e6 ? "$" + (v / 1e6).toFixed(2) + "M" : "$" + Math.round(v / 1e3) + "k";
  }

  function transactionsTable(txns) {
    if (!txns.length) return `<p class="p-empty">No transactions in the selected period.</p>`;
    const rows = [...txns].reverse().slice(0, 12).map((t) => `
      <tr>
        <td>${monthLabel(t.date)}</td>
        <td class="num">${money(t.price)}</td>
        <td class="num">${t.area_sqft ? num.format(Math.round(t.area_sqft)) : "—"}</td>
        <td class="num">${psfText(t.psf)}</td>
        <td>${escapeHtml(t.storey || "—")}</td>
      </tr>`).join("");
    return `<div class="p-table-wrap"><table class="p-table">
      <thead><tr><th>Month</th><th class="num">Price</th><th class="num">Sqft</th>
      <th class="num">PSF</th><th>Storey</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  // ── chart ─────────────────────────────────────────────────────────────

  function drawChart(txns) {
    const canvas = $("psfChart");
    if (!canvas) return;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
    if (!txns.length) return;

    const series = monthlyMedians(txns);
    if (!series.length) return;
    const growthMode = state.measure === "growth";
    const labels = series.map((s) => s[0]);
    // Rebased to the first month in the selected period, so the line answers
    // "how much has this moved since then" rather than "what does it cost".
    const base = series[0][1];
    const values = series.map((s) => (growthMode
      ? +(((s[1] / base) - 1) * 100).toFixed(1)
      : Math.round(s[1])));

    const accent = cssVar("--accent");
    const ink = cssVar("--text-secondary");
    const muted = cssVar("--text-muted");
    const grid = cssVar("--gridline");
    const surface = cssVar("--surface-1");

    state.chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Median psf",
          data: values,
          borderColor: accent,
          borderWidth: 2,
          backgroundColor: cssVar("--accent-soft"),
          fill: true,
          tension: 0.25,
          pointRadius: labels.length > 40 ? 0 : 3,
          pointHoverRadius: 7,
          pointBackgroundColor: surface,
          pointBorderColor: accent,
          pointBorderWidth: 2,
          pointHitRadius: 14,          // generous hit target, not the 3px dot
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },   // single series — the heading names it
          tooltip: {
            backgroundColor: surface,
            titleColor: cssVar("--text-primary"),
            bodyColor: ink,
            borderColor: grid,
            borderWidth: 1,
            padding: 9,
            displayColors: false,
            callbacks: {
              title: (items) => monthLabel(items[0].label),
              label: (item) => {
                if (!growthMode) return psfText(item.parsed.y) + " psf";
                const raw = series[item.dataIndex][1];
                return `${pct(item.parsed.y / 100)} (${psfText(raw)})`;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            border: { color: grid },
            ticks: {
              color: muted, font: { size: 10 }, maxRotation: 0, autoSkipPadding: 18,
              callback(value, index) { return shortMonth(this.getLabelForValue(index)); },
            },
          },
          y: {
            grid: {
              drawTicks: false,
              color: (ctx) => (growthMode && ctx.tick.value === 0
                ? cssVar("--baseline") : grid),
            },
            border: { display: false },
            ticks: {
              color: muted, font: { size: 10 }, padding: 6, maxTicksLimit: 5,
              callback: (v) => (growthMode
                ? (v > 0 ? "+" : "") + v + "%"
                : "$" + num.format(v)),
            },
          },
        },
      },
    });
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ── controls ──────────────────────────────────────────────────────────

  function syncRangeUI() {
    const last = state.months.length - 1 || 1;
    el.rangeStart.value = state.startIdx;
    el.rangeEnd.value = state.endIdx;
    el.drFill.style.left = (state.startIdx / last) * 100 + "%";
    el.drFill.style.width = ((state.endIdx - state.startIdx) / last) * 100 + "%";
    const [from, to] = rangeBounds();
    el.rangeLabel.textContent = `${monthLabel(from)} – ${monthLabel(to)}`;
  }

  /** The month a preset starts at, or null when the history is too short —
   *  a 10Y button on 9 years of data would just be "All" under another name. */
  function presetStart(preset) {
    const months = state.months;
    if (!months.length) return null;
    if (preset.id === "all") return 0;

    const last = months[months.length - 1];
    const target = preset.id === "ytd"
      ? `${last.slice(0, 4)}-01-01`
      : `${+last.slice(0, 4) - preset.years}${last.slice(4)}`;

    if (target <= months[0]) return null;           // not enough history
    const idx = months.findIndex((m) => m >= target);
    return idx === -1 ? null : idx;
  }

  function buildPresets() {
    el.presets.innerHTML = PERIOD_PRESETS.map((p) => {
      const start = presetStart(p);
      const off = start === null;
      return `<button type="button" class="preset${off ? " is-off" : ""}"
        data-preset="${p.id}" ${off ? "disabled" : ""} aria-pressed="false"
        ${off ? `title="Not enough history for ${p.label}"` : ""}>${p.label}</button>`;
    }).join("");

    for (const btn of el.presets.querySelectorAll(".preset")) {
      btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
    }
  }

  function applyPreset(id) {
    const preset = PERIOD_PRESETS.find((p) => p.id === id);
    const start = preset && presetStart(preset);
    if (start === null || start === undefined) return;
    stopPlay();                       // a preset is an explicit choice
    state.startIdx = start;
    state.endIdx = state.months.length - 1;
    applyRange();
  }

  /** Light whichever preset the current range happens to equal, including
   *  after dragging a thumb onto one — the slider and the buttons are two
   *  views of one range, not two separate controls. */
  function syncPresets() {
    const last = state.months.length - 1;
    for (const btn of el.presets.querySelectorAll(".preset")) {
      const preset = PERIOD_PRESETS.find((p) => p.id === btn.dataset.preset);
      const start = preset ? presetStart(preset) : null;
      const on = start !== null && state.startIdx === start && state.endIdx === last;
      btn.classList.toggle("is-on", on);
      btn.setAttribute("aria-pressed", String(on));
    }
  }

  function applyRange() {
    syncRangeUI();
    syncPresets();
    applyFilters();
  }

  function onRangeInput() {
    let a = +el.rangeStart.value, b = +el.rangeEnd.value;
    if (a > b) [a, b] = [b, a];          // let either thumb pass the other
    state.startIdx = a;
    state.endIdx = b;
    applyRange();
  }

  /** An empty box means "no limit". Zero is a real bound, so `|| null` would
   *  be wrong here — only a blank or unparseable value clears the filter. */
  function numOrNull(input) {
    const raw = input.value.trim();
    if (raw === "") return null;
    const n = Number(raw);
    return Number.isFinite(n) && n >= 0 ? n : null;
  }

  function readValueFilters() {
    state.minSqft = numOrNull(el.minSqft);
    state.maxSqft = numOrNull(el.maxSqft);
    state.minPrice = numOrNull(el.minPrice);
    state.maxPrice = numOrNull(el.maxPrice);
    applyFilters();
  }

  function onLeaseInput() {
    state.minLease = +el.minLease.value;
    syncLeaseUI();
    applyFilters();
  }

  function syncLeaseUI() {
    const max = state.leaseMax || 99;
    el.leaseLabel.textContent =
      state.minLease > 0 ? `${state.minLease}+ years` : "Any";
    el.leaseFill.style.width = (state.minLease / max) * 100 + "%";
  }

  /** Re-render everything the filters scope, and keep the badge honest. */
  function applyFilters() {
    updateModelCounts();
    const n = activeFilterCount();
    el.filterCount.textContent = String(n);
    el.filterCount.hidden = n === 0;
    renderMarkers();
    refreshDetailViews();
  }

  /** Re-read whatever detail view is open. Both filter paths call this, so
   *  neither can refresh the drawer and forget the panel — which is exactly
   *  how Reset came to leave stale numbers on screen, twice. */
  function refreshDetailViews() {
    if (state.selectedId) {
      // The selected property may no longer pass the filters.
      const still = visibleProperties().some((p) => p.id === state.selectedId);
      still ? renderPanel() : closePanel();
    }
    // Comparison is scoped by the filters too. The selection itself is
    // deliberately kept — a filter that hides a compared property shows an
    // empty column rather than dropping the choice.
    if (state.compareMode) renderCompare();
  }

  const filtersOpen = () => getComputedStyle(el.moreFilters).display !== "none";

  /** Reads the row's actual rendered state rather than tracking its own, so
   *  the CSS default and an explicit toggle can never disagree. */
  function toggleMoreFilters() {
    const next = !filtersOpen();
    document.body.classList.toggle("filters-open", next);
    document.body.classList.toggle("filters-closed", !next);
    el.moreToggle.setAttribute("aria-expanded", String(next));
    // The row changes the header's height, so the map box changed size — but
    // no window resize fired, and Leaflet caches its size.
    if (map) {
      map.invalidateSize({ animate: false });
      updateLabels();
    }
  }

  function syncFiltersAria() {
    el.moreToggle.setAttribute("aria-expanded", String(filtersOpen()));
  }

  /** Chips come from the data, so the watchlist can change without touching
   *  the markup. Built once; only the counts are rewritten afterwards, so
   *  clicking never rebuilds the row underneath the pointer. */
  function buildModelChips() {
    const all = new Set();
    for (const p of state.properties) {
      const m = modelOf(p);
      if (m) all.add(m);
    }
    state.allModels = [...all].sort((a, b) => a.localeCompare(b));
    if (state.allModels.length < 2) return;      // nothing to choose between

    el.modelChips.innerHTML =
      `<button type="button" class="chip is-on" data-model="" aria-pressed="true">All</button>` +
      state.allModels.map((m) =>
        `<button type="button" class="chip" data-model="${escapeHtml(m)}" aria-pressed="false"
           >${escapeHtml(m)}<span class="chip-count" data-count="${escapeHtml(m)}">–</span></button>`
      ).join("");

    for (const chip of el.modelChips.querySelectorAll(".chip")) {
      chip.addEventListener("click", () => toggleModel(chip.dataset.model));
    }
    updateModelCounts();
  }

  /** How many properties each model would give you *right now*.
   *
   *  Every other filter is applied, but the model selection itself is not —
   *  otherwise picking one model would zero every other chip and there would
   *  be nothing left to navigate by. This is the standard faceted-count rule:
   *  a chip's number is what you would get if you added it to the filters you
   *  already have. */
  function modelCounts() {
    const counts = new Map(state.allModels.map((m) => [m, 0]));
    for (const p of state.properties) {
      if (state.source !== "ALL" && p.source !== state.source) continue;
      if (state.minLease > 0) {
        const left = yearsLeft(p);
        if (left != null && left < state.minLease) continue;
      }
      // Period, size and price live on transactions, so a property only
      // counts if at least one of its transactions survives them.
      if (!matchingTxns(p).length) continue;
      const m = modelOf(p);
      if (counts.has(m)) counts.set(m, counts.get(m) + 1);
    }
    return counts;
  }

  function updateModelCounts() {
    if (!state.allModels.length) return;
    const counts = modelCounts();
    for (const chip of el.modelChips.querySelectorAll(".chip[data-model]")) {
      const model = chip.dataset.model;
      if (!model) continue;                       // the "All" chip carries none
      const n = counts.get(model) ?? 0;
      const span = chip.querySelector(".chip-count");
      if (span) span.textContent = String(n);
      // Dimmed, not disabled: it stays selectable so the empty state explains
      // itself rather than the chip just refusing to respond.
      chip.classList.toggle("is-zero", n === 0);
      chip.title = n === 0
        ? `${model} — nothing matches the other filters`
        : `${n} propert${n === 1 ? "y" : "ies"} with the current filters`;
    }
  }

  function toggleModel(model) {
    if (!model) {
      state.models.clear();                      // the "All" chip
    } else if (state.models.has(model)) {
      state.models.delete(model);
    } else {
      state.models.add(model);
    }
    syncModelChips();
    applyFilters();
  }

  function syncModelChips() {
    for (const chip of el.modelChips.querySelectorAll(".chip")) {
      const m = chip.dataset.model;
      const on = m ? state.models.has(m) : state.models.size === 0;
      chip.classList.toggle("is-on", on);
      chip.setAttribute("aria-pressed", String(on));
    }
  }

  function setSource(source) {
    state.source = source;
    // Scoped to the source group: a bare ".chip" also matches the model chips,
    // whose dataset has no `source`, so `undefined === undefined` lit every
    // one of them up and set the source to undefined.
    for (const chip of el.sourceChips.querySelectorAll(".chip")) {
      const on = chip.dataset.source === source;
      chip.classList.toggle("is-on", on);
      chip.setAttribute("aria-pressed", String(on));
    }
    if (state.selectedId) {
      const prop = state.properties.find((p) => p.id === state.selectedId);
      if (prop && source !== "ALL" && prop.source !== source) closePanel();
    }
    const n = activeFilterCount();
    el.filterCount.textContent = String(n);
    el.filterCount.hidden = n === 0;
    updateModelCounts();
    renderMarkers({ fit: true });
    // This is the one filter path that doesn't go through applyFilters, and
    // Reset ends here — without this the open detail views keep showing the
    // numbers from before the reset.
    refreshDetailViews();
  }

  // Sweeps a fixed-width window forward through time, then loops.
  function togglePlay() {
    state.playing ? stopPlay() : startPlay();
  }

  function startPlay() {
    const last = state.months.length - 1;
    if (last < 1) return;
    let width = state.endIdx - state.startIdx;
    if (width < 1 || width >= last) width = Math.max(1, Math.round((last + 1) / 4));

    state.playing = true;
    el.playGlyph.textContent = "❚❚";
    el.playText.textContent = "Pause";
    state.startIdx = 0;
    state.endIdx = width;
    applyRange();

    state.timer = setInterval(() => {
      if (state.endIdx >= last) {
        state.startIdx = 0;
        state.endIdx = width;
      } else {
        state.startIdx += 1;
        state.endIdx += 1;
      }
      applyRange();
    }, 420);
  }

  function stopPlay() {
    state.playing = false;
    clearInterval(state.timer);
    state.timer = null;
    el.playGlyph.textContent = "▶";
    el.playText.textContent = "Play";
  }

  function resetView() {
    stopPlay();
    state.startIdx = 0;
    state.endIdx = state.months.length - 1;
    state.minSqft = state.maxSqft = state.minPrice = state.maxPrice = null;
    state.minLease = 0;
    state.models.clear();
    syncModelChips();
    for (const input of [el.minSqft, el.maxSqft, el.minPrice, el.maxPrice]) {
      input.value = "";
    }
    el.minLease.value = "0";
    syncLeaseUI();
    syncRangeUI();
    syncPresets();
    setSource("ALL");                 // renders and refits
  }

  // ── boot ──────────────────────────────────────────────────────────────

  async function boot() {
    initMap();

    let data;
    try {
      const res = await fetch("data.json", { cache: "no-cache" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      data = await res.json();
    } catch (err) {
      el.meta.textContent = "Could not load data.json — run `python -m ingest.run` first.";
      console.error(err);
      return;
    }

    state.properties = (data.properties || []).filter((p) => p.txns && p.txns.length);
    state.generatedAt = data.generated_at ? data.generated_at.slice(0, 10) : null;

    if (!state.properties.length) {
      el.meta.textContent = "No transactions yet — add properties to config/watchlist.yaml.";
      return;
    }

    const months = new Set();
    const allPsf = [];
    for (const p of state.properties) {
      p.txns.sort((a, b) => a.date.localeCompare(b.date));
      for (const t of p.txns) {
        months.add(t.date);
        if (t.psf != null) allPsf.push(t.psf);
      }
    }
    state.months = [...months].sort();
    state.thresholds = buildThresholds(allPsf);

    const last = state.months.length - 1;
    for (const input of [el.rangeStart, el.rangeEnd]) input.max = String(last);
    state.startIdx = 0;
    state.endIdx = last;

    // Cap the lease slider at the longest lease actually present, so the top
    // of the track isn't dead travel. Freehold contributes no bound.
    const leases = state.properties.map(yearsLeft).filter((v) => v != null);
    state.leaseMax = leases.length
      ? Math.max(5, Math.ceil(Math.max(...leases) / 5) * 5)
      : 99;
    el.minLease.max = String(state.leaseMax);

    renderLegend();
    buildModelChips();
    buildPresets();
    syncRangeUI();
    syncPresets();
    syncLeaseUI();
    syncFiltersAria();
    renderMarkers({ fit: true });

    el.rangeStart.addEventListener("input", onRangeInput);
    el.rangeEnd.addEventListener("input", onRangeInput);
    for (const input of [el.minSqft, el.maxSqft, el.minPrice, el.maxPrice]) {
      input.addEventListener("input", readValueFilters);
    }
    el.minLease.addEventListener("input", onLeaseInput);
    el.moreToggle.addEventListener("click", toggleMoreFilters);
    el.schoolsToggle.addEventListener("click", toggleSchools);
    el.compareToggle.addEventListener("click", toggleCompare);
    el.compareClear.addEventListener("click", clearCompare);
    el.compareClose.addEventListener("click", toggleCompare);
    el.compareSearch.addEventListener("input", onSearchInput);
    el.compareSearch.addEventListener("keydown", onSearchKey);
    for (const chip of el.cmpModeChips.querySelectorAll(".chip")) {
      chip.addEventListener("click", () => setMeasure(chip.dataset.cmpmode));
    }
    el.compareSearch.addEventListener("blur", () => setTimeout(hideResults, 120));
    renderCompare();
    el.play.addEventListener("click", togglePlay);
    el.reset.addEventListener("click", resetView);
    el.panelClose.addEventListener("click", closePanel);
    el.scrim.addEventListener("click", closePanel);
    for (const chip of el.sourceChips.querySelectorAll(".chip")) {
      chip.addEventListener("click", () => setSource(chip.dataset.source));
    }
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !el.panel.hidden) closePanel();
    });
    // Dark mode uses its own colour steps, so the chart is rebuilt rather than
    // inheriting a filtered version of the light one.
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      renderMarkers();
      if (state.selectedId) renderPanel();
    });
    addEventListener("resize", () => {
      map.invalidateSize({ animate: false });
      syncFiltersAria();
    });

    // Optional layer: an older deploy, or a run with --skip-schools, simply
    // has no schools.json. Hide the control rather than erroring.
    try {
      const res = await fetch("schools.json", { cache: "no-cache" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      state.schools = (await res.json()).schools || [];
    } catch (err) {
      console.warn("no schools.json — school layer disabled", err);
    }
    if (!state.schools.length) {
      el.schoolsToggle.hidden = true;
    } else {
      el.schoolsToggle.title =
        `Show ${state.schools.length} primary schools and their P1 distance rings`;
    }
  }

  boot();
})();
