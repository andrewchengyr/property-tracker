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
    rangeLabel: $("rangeLabel"), drFill: $("drFill"),
    reset: $("reset"),
    moreFilters: $("moreFilters"), moreToggle: $("moreToggle"),
    filterCount: $("filterCount"), emptyNote: $("emptyNote"),
    minSqft: $("minSqft"), maxSqft: $("maxSqft"),
    minPrice: $("minPrice"), maxPrice: $("maxPrice"),
    minLease: $("minLease"), leaseLabel: $("leaseLabel"), leaseFill: $("leaseFill"),
    leaseHist: $("leaseHist"), leaseFh: $("leaseFh"),
    modelChips: $("modelChips"), sourceChips: $("sourceChips"),
    schoolsToggle: $("schoolsToggle"), legendSchool: $("legendSchool"),
    landToggle: $("landToggle"), legendLand: $("legendLand"), legendLu: $("legendLu"),
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
    schools: [], showSchools: false,
    // Multi-select: the catchment question is about overlap, so one school
    // is just the one-element case rather than a separate mode.
    schoolPicks: [], schoolScope: "all",
    // The land-use overlay is ~700 KB, so it is fetched on first use and then
    // kept — `land` null means "not fetched yet", not "empty".
    land: null, showLand: false, landPending: false,
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
  // The period the map opens on, and the one Reset returns to. Recent prices
  // are the ones worth looking at first; the full history is one chip away.
  // Falls back to the full range when there isn't a year of data yet.
  const DEFAULT_PERIOD = "1y";

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

  /** Cumulative change and its annualised equivalent at one point in a series.
   *
   *  Same rule as the headline growth figure: under a year there is no annual
   *  rate to quote, because annualising four months of data extrapolates it to
   *  a year it hasn't lived through. At the final point this returns exactly
   *  what growth() reports, which is the point of sharing the maths. */
  function growthAt(base, value, baseMonth, month) {
    const cum = value / base - 1;
    const years = (Date.parse(month) - Date.parse(baseMonth)) / MS_PER_YEAR;
    return {
      cum,
      years,
      annual: years >= 1 ? Math.pow(1 + cum, 1 / years) - 1 : null,
    };
  }

  function pct(v) {
    if (v == null) return "—";
    const s = (v * 100).toFixed(1);
    return (v > 0 ? "+" : "") + s + "%";
  }

  /** Gross rental yield: a year of rent as a share of capital value.
   *
   *      annual rent psf / sale psf
   *
   *  Both sides are per sqft, so floor area cancels and an HDB block quoted
   *  per unit means the same thing as a condo quoted per sqft.
   *
   *  **Gross, not net** — no maintenance, tax, agent fee or vacancy. And the
   *  rent window rarely matches the price window: URA publishes quarterly
   *  medians only from 2023, HDB monthly from 2021, so a 10-year price filter
   *  has no rent to pair with its early years. Rather than silently blending
   *  2019 prices with 2024 rents, the rent is taken from the overlap and the
   *  span actually used is reported back for the caption to state. */
  function rentalYield(prop, txns) {
    const rents = prop.rents || [];
    if (!rents.length || !txns.length) return null;

    const psf = medianPsf(txns);
    if (!psf) return null;

    const from = txns[0].date;
    const to = txns[txns.length - 1].date;
    let window = rents.filter((r) => r.date >= from && r.date <= to);

    // No overlap at all — the price period predates published rents. Fall back
    // to the most recent four quarters and say so, because a yield from the
    // freshest rent is more use than no yield.
    const matched = window.length > 0;
    if (!matched) window = rents.slice(-4);
    if (!window.length) return null;

    const rent = median(window.map((r) => r.psf));
    if (!rent) return null;

    return {
      value: (rent * 12) / psf,
      rentPsf: rent,
      psf,
      matched,
      months: window.length,
      contracts: window.reduce((a, r) => a + (r.n || 0), 0),
      firstDate: window[0].date,
      lastDate: window[window.length - 1].date,
    };
  }

  /** Where the price trend sits: Momentum (rising), Peaked (stabilised),
   *  Cooling (falling).
   *
   *  This describes prices that have already happened. It is not a forecast,
   *  and it is deliberately built so it cannot pretend to be one.
   *
   *  A least-squares line is fitted to log(psf) against time, over the monthly
   *  medians — logs because a straight line in log space *is* a constant
   *  percentage growth rate, which is what "rising" means for a price. The
   *  slope converts directly to an annual rate.
   *
   *  The classification then turns on statistical significance rather than an
   *  invented threshold. If the slope's t-statistic can't clear 2 — roughly a
   *  95% interval that excludes zero — the trend is not distinguishable from
   *  flat, and flat is what gets reported. Thin or noisy histories therefore
   *  land on "Peaked" rather than being talked into a direction, and anything
   *  under 6 months of sales or 18 months of span gets no verdict at all. */
  const PHASE_MIN_MONTHS = 6;
  const PHASE_MIN_SPAN_YEARS = 1.5;
  const PHASE_T = 2;

  function phase(txns) {
    const series = monthlyMedians(txns).filter(([, v]) => v > 0);
    if (series.length < PHASE_MIN_MONTHS) return null;

    const t0 = Date.parse(series[0][0]);
    const xs = series.map(([d]) => (Date.parse(d) - t0) / MS_PER_YEAR);
    const ys = series.map(([, v]) => Math.log(v));
    const span = xs[xs.length - 1];
    if (span < PHASE_MIN_SPAN_YEARS) return null;

    const n = xs.length;
    const mx = xs.reduce((a, b) => a + b, 0) / n;
    const my = ys.reduce((a, b) => a + b, 0) / n;
    let sxx = 0, sxy = 0;
    for (let i = 0; i < n; i++) {
      sxx += (xs[i] - mx) ** 2;
      sxy += (xs[i] - mx) * (ys[i] - my);
    }
    if (sxx <= 0) return null;

    const slope = sxy / sxx;
    const intercept = my - slope * mx;

    // Residual spread gives the standard error of the slope, and with it the
    // honest answer to "could this line just as easily be flat?"
    let sse = 0, sst = 0;
    for (let i = 0; i < n; i++) {
      sse += (ys[i] - (intercept + slope * xs[i])) ** 2;
      sst += (ys[i] - my) ** 2;
    }
    const se = Math.sqrt(sse / (n - 2) / sxx);
    const t = se > 0 ? slope / se : 0;

    const significant = Math.abs(t) >= PHASE_T;
    const key = !significant ? "peaked" : slope > 0 ? "momentum" : "cooling";

    return {
      key,
      label: { momentum: "Momentum", peaked: "Peaked", cooling: "Cooling" }[key],
      annual: Math.exp(slope) - 1,     // the fitted rate, shown either way
      t,
      significant,
      r2: sst > 0 ? Math.max(0, 1 - sse / sst) : 0,
      months: n,
      years: span,
      firstDate: series[0][0],
      lastDate: series[series.length - 1][0],
    };
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

  /** Several HDB flat models answer the same question, so the chips group them.
   *
   *  `Improved`, `Model A` and `Standard` are HDB's generational names for the
   *  ordinary flat — nobody shortlists by which decade the layout dates from —
   *  and a maisonette is a maisonette whether or not HDB called it `Model A`.
   *  Collapsing them turns eleven chips into seven and puts the counts where a
   *  decision actually gets made.
   *
   *  Grouping is a *filtering* decision only. Detail views (the panel, compare
   *  columns, the search list) keep showing the exact model, the same way the
   *  land-use layer buckets its colours but names the precise use on hover —
   *  the grouping must never cost you the underlying fact. */
  const MODEL_GROUPS = {
    "Improved": "HDB",
    "Model A": "HDB",
    "Standard": "HDB",
    "Maisonette": "Maisonette",
    "Model A-Maisonette": "Maisonette",
  };

  /** Property-level filters. Lease is a fact about the building, not about any
   *  one transaction, so it hides the property outright. Freehold and
   *  unknown-lease properties pass any minimum — a freehold outlasts every
   *  threshold, and hiding what we can't assess would quietly lose data. */
  const modelOf = (p) => {
    const raw = p.model || p.flat_model || p.type || "";
    return MODEL_GROUPS[raw] || raw;
  };

  /** Property-level filters, with one optionally skipped.
   *
   *  Skipping is what makes a facet honest: a model chip's count and the lease
   *  histogram each have to ignore their own filter, or selecting a value
   *  would zero everything else and there'd be nothing left to navigate by.
   *  One function so the three callers can't drift apart. */
  function passesProperty(p, skip) {
    if (skip !== "source" && state.source !== "ALL" && p.source !== state.source) {
      return false;
    }
    if (skip !== "model" && state.models.size && !state.models.has(modelOf(p))) {
      return false;
    }
    if (skip !== "lease" && state.minLease > 0) {
      const left = yearsLeft(p);
      // Freehold and unknown-lease pass any minimum: a freehold outlasts every
      // threshold, and hiding what can't be assessed would quietly lose data.
      if (left != null && left < state.minLease) return false;
    }
    return true;
  }

  function visibleProperties() {
    return state.properties.filter((p) => passesProperty(p));
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

  /** One writer for the badge, called by both filter paths *and* by boot —
   *  the period no longer starts at the full range, so a badge that was only
   *  written on the first interaction would sit empty over a filtered map and
   *  then pop to 1 the moment anything was touched. */
  function syncFilterBadge() {
    const n = activeFilterCount();
    el.filterCount.textContent = String(n);
    el.filterCount.hidden = n === 0;
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
    // Land parcels under the rings, rings under the school markers, all of
    // them under the property markers — the properties are the subject and
    // everything else is context. Added in that order because layers within
    // the overlay pane stack by insertion.
    landLayer = L.layerGroup().addTo(map);
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

  /** Sits above the point, flipping below when there isn't room and clamping
   *  to the stage horizontally, so it is never clipped at an edge.
   *
   *  Takes a container point rather than a marker: property markers and land
   *  parcels both hover, and one implementation of "flip and clamp" is the
   *  point — two would drift apart at exactly the edges nobody tests. */
  function placeHoverCard(html, pt) {
    const node = el.hoverCard;
    if (!node) return;        // cached older index.html — degrade, don't break
    node.innerHTML = html;
    node.hidden = false;

    const stage = el.stage.getBoundingClientRect();
    const card = node.getBoundingClientRect();

    let top = pt.y - card.height - HOVER_GAP;
    if (top < 6) top = pt.y + HOVER_GAP + 12;             // flip below
    let left = pt.x - card.width / 2;
    left = Math.max(6, Math.min(left, stage.width - card.width - 6));

    node.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  }

  function showHoverCard(prop, txns, psf, marker) {
    placeHoverCard(hoverCard(prop, txns, psf),
                   map.latLngToContainerPoint(marker.getLatLng()));
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

    const ph = phase(txns);
    const y = rentalYield(prop, txns);

    const rows = [
      ["Median psf", psfText(medianPsf(txns))],
      ["Growth", g ? `${pct(g.annual != null ? g.annual : g.total)}` +
        `<span class="cmp-sub">${g.annual != null ? "per year" : "over period"}</span>` : "—"],
      // Same two metrics as the panel, on the same period — the whole point of
      // Compare is that nothing is computed differently here.
      ["Phase", ph
        ? `<span class="cmp-phase is-${ph.key}">${ph.label}</span>` +
          `<span class="cmp-sub">${pct(ph.annual)} a year fitted</span>`
        : `—<span class="cmp-sub">too few months</span>`],
      ["Gross yield", y
        ? `${(y.value * 100).toFixed(2)}%<span class="cmp-sub">` +
          `${y.rentPsf.toFixed(2)} psf/mo rent</span>`
        : `—<span class="cmp-sub">no rent published</span>`],
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
                const month = months[item.dataIndex];
                const raw = s.points.get(month);
                if (raw == null || s.base == null) return `${item.dataset.label}: no sale`;
                const g = growthAt(s.base, raw, s.baseMonth, month);
                return `${item.dataset.label}: ${pct(g.cum)}`
                  + (g.annual != null ? ` · ${pct(g.annual)} p.a.` : "")
                  + ` · ${psfText(raw)}`;
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

  // ── master plan land use ──────────────────────────────────────────────

  let landLayer, landGeo = null;

  /** Residential is the ground, not one of the categories.
   *
   *  It is 82% of parcels in these areas, and the map's whole subject is
   *  residential property — so painting it as one of six competing hues would
   *  spend the palette's scarcest resource on its least informative class.
   *  Drawn as a quiet tint the basemap reads through, which also keeps most of
   *  the map's streets legible while the layer is on. The six exception
   *  buckets are near-opaque because the measurement said so: composited at
   *  40% over the basemap, the worst pair of any six-colour set falls to
   *  ΔE 9.7 against a hard floor of 15 — a translucent wash of this many
   *  categories is not a readable encoding at any hue. */
  const LU_GROUND = "homes";
  const LU_GROUND_OPACITY = 0.34;
  const LU_FILL_OPACITY = 0.82;

  const luColour = (bucket) => cssVar(`--lu-${bucket}`) || cssVar("--lu-other");

  function luStyle(feature) {
    const bucket = feature.properties.b;
    const ground = bucket === LU_GROUND;
    return {
      fillColor: luColour(bucket),
      fillOpacity: ground ? LU_GROUND_OPACITY : LU_FILL_OPACITY,
      // A hairline in the surface colour separates two parcels of the same
      // use, which would otherwise merge into one shapeless blob. The ground
      // gets none: 5,000 landed lots' worth of hairlines is a grid, not a map.
      color: cssVar("--surface-1"),
      weight: ground ? 0 : 0.5,
      opacity: ground ? 0 : 0.45,
      interactive: true,
    };
  }

  /** Gross plot ratio, or the plan's shorthand where it isn't a number. */
  function gprHtml(gpr, codes) {
    if (!gpr) return "";
    const decoded = codes[gpr];
    if (decoded) return `<p class="lu-gpr">${escapeHtml(decoded)}</p>`;
    if (!Number.isFinite(Number(gpr))) {
      return `<p class="lu-gpr">Plot ratio <b>${escapeHtml(gpr)}</b></p>`;
    }
    return `<p class="lu-gpr">Plot ratio <b>${escapeHtml(gpr)}</b>
      <span class="lu-note">— floor area allowed per unit of land</span></p>`;
  }

  /** The exact land use, never just the bucket. The six buckets are a drawing
   *  decision made to keep the palette readable; the plan's own wording is
   *  what the reader actually came for, so it leads. */
  function landCard(props) {
    const codes = (state.land && state.land.gpr_codes) || {};
    const labels = luLabels();
    return `<div class="hover-card">
      <div class="lu-head">
        <i class="lu-swatch" style="background:${luColour(props.b)}" aria-hidden="true"></i>
        <span class="lu-bucket">${escapeHtml(labels[props.b] || "Land use")}</span>
      </div>
      <p class="lu-use">${escapeHtml(props.lu || "Not stated")}</p>
      ${gprHtml(props.gpr, codes)}
      <p class="lu-note">URA Master Plan 2025 · zoning, not what is built today</p>
    </div>`;
  }

  function luLabels() {
    const out = {};
    for (const row of (state.land && state.land.legend) || []) out[row.key] = row.label;
    return out;
  }

  function toggleLand() {
    if (state.landPending) return;
    if (!state.land) return loadLand();       // fetch, then turn on
    setLand(!state.showLand);
  }

  function setLand(on) {
    state.showLand = on;
    el.landToggle.classList.toggle("is-on", on);
    el.landToggle.setAttribute("aria-pressed", String(on));
    if (el.legendLand) el.legendLand.hidden = !on;
    renderLand();
  }

  async function loadLand() {
    state.landPending = true;
    el.landToggle.classList.add("is-busy");
    el.landToggle.disabled = true;
    try {
      const res = await fetch("masterplan.json", { cache: "force-cache" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      state.land = await res.json();
      renderLandLegend();
      setLand(true);
    } catch (err) {
      // A missing or broken overlay costs the map nothing else — drop the
      // control rather than leaving a button that can only fail.
      console.warn("no masterplan.json — land-use layer disabled", err);
      el.landToggle.hidden = true;
    } finally {
      state.landPending = false;
      el.landToggle.classList.remove("is-busy");
      el.landToggle.disabled = false;
    }
  }

  function renderLand() {
    landLayer.clearLayers();
    landGeo = null;
    if (!state.showLand || !state.land) return;

    // Canvas, not SVG: 6,000-odd parcels as DOM nodes stalls the whole map on
    // a phone, and these are fills with no per-parcel interaction beyond a
    // hover. Rebuilt on toggle so a dark-mode switch re-reads the colours.
    landGeo = L.geoJSON(
      { type: "FeatureCollection", features: state.land.features },
      { style: luStyle, renderer: L.canvas({ padding: 0.3 }) },
    );

    landGeo.on("mouseover", (e) => {
      if (!CAN_HOVER) return;
      placeHoverCard(landCard(e.layer.feature.properties), e.containerPoint);
    });
    landGeo.on("mouseout", hideHoverCard);
    landGeo.addTo(landLayer);
  }

  /** Only the buckets actually present, so the legend describes this map
   *  rather than the plan's full vocabulary. */
  function renderLandLegend() {
    if (!el.legendLu || !state.land) return;
    const counts = state.land.counts || {};
    el.legendLu.innerHTML = (state.land.legend || [])
      .filter((row) => counts[row.key])
      .map((row) => `<span class="legend-lu-row"
          title="${counts[row.key]} parcel${counts[row.key] === 1 ? "" : "s"}">
          <i class="legend-lu-sw" style="background:${luColour(row.key)}"
             aria-hidden="true"></i><span>${escapeHtml(row.label)}</span></span>`)
      .join("");
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
      const selected = isPicked(school);
      const marker = L.marker([school.lat, school.lng], {
        icon: L.divIcon({
          className: "sk-wrap" + (selected ? " is-selected" : ""),
          html: `<span class="sk" title="${escapeHtml(school.name)}"></span>`,
          iconSize: [18, 18],
          iconAnchor: [9, 9],
        }),
        title: `${school.name} — click to ${selected ? "remove from" : "add to"} the catchment comparison`,
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

  const isPicked = (school) =>
    state.schoolPicks.some((s) => s.postal === school.postal);

  /** Clicking a school toggles it in or out of the comparison set.
   *
   *  Toggling rather than replacing is what makes the intersection question
   *  askable at all, and it keeps the escape hatch: clicking the only picked
   *  school again clears everything. */
  function selectSchool(school) {
    state.schoolPicks = isPicked(school)
      ? state.schoolPicks.filter((s) => s.postal !== school.postal)
      : [...state.schoolPicks, school];

    if (!state.schoolPicks.length) {
      clearSchoolSelection();
      renderSchools();
      return;
    }
    state.selectedId = null;          // the panel shows one thing at a time
    drawRings(state.schoolPicks);
    renderSchools();
    renderSchoolPanel();
    renderMarkers();
  }

  function clearSchoolSelection() {
    state.schoolPicks = [];
    ringLayer.clearLayers();
    if (!state.selectedId) closePanel();
  }

  function drawRings(schools) {
    ringLayer.clearLayers();
    const points = [];
    for (const school of schools) {
      const centre = [school.lat, school.lng];
      points.push(centre);
      // Outer first so the 1 km ring paints over it.
      for (const radius of [...P1_BANDS].reverse()) {
        L.circle(centre, {
          radius,
          className: radius === 1000 ? "ring ring--1km" : "ring ring--2km",
          interactive: false,
        }).addTo(ringLayer);
      }
    }
    if (!points.length) return;

    // latLng.toBounds() takes the box's full width in metres and needs no map.
    // Circle.getBounds() would be the obvious call, but it reads this._map and
    // throws on a circle that hasn't been added yet.
    const outer = P1_BANDS[P1_BANDS.length - 1];
    let bounds = L.latLng(points[0]).toBounds(outer * 2.4);
    for (const pt of points.slice(1)) {
      bounds = bounds.extend(L.latLng(pt).toBounds(outer * 2.4));
    }
    map.fitBounds(bounds, { maxZoom: 16 });
  }

  const OUTER_BAND = P1_BANDS[P1_BANDS.length - 1];

  /** Watched properties measured against every picked school.
   *
   *  `scope` decides what qualifies: "all" keeps only properties inside the
   *  outer band of *every* school — the intersection, which is the point of
   *  picking more than one — while "any" keeps a property near at least one.
   *
   *  Ranked by the **farthest** school, not the nearest. A property 200 m from
   *  one school and 1.9 km from the other is a worse joint catchment than one
   *  sitting 1.1 km from both, and ranking on the nearest would put it first.
   *
   *  Runs over the visible set, so every other filter still applies. */
  function propertiesNearSchools(schools, scope = state.schoolScope) {
    const out = [];
    for (const prop of visibleProperties()) {
      if (prop.lat == null || prop.lng == null) continue;
      const txns = matchingTxns(prop);
      if (!txns.length) continue;

      const dists = schools.map((s) => distanceM(s.lat, s.lng, prop.lat, prop.lng));
      const worst = Math.max(...dists);
      const best = Math.min(...dists);
      const qualifies = scope === "all" ? worst <= OUTER_BAND : best <= OUTER_BAND;
      if (!qualifies) continue;

      out.push({
        prop, dists, worst, best, txns,
        within1: dists.filter((d) => d <= P1_BANDS[0]).length,
      });
    }
    return out.sort((a, b) => (scope === "all" ? a.worst - b.worst : a.best - b.best));
  }

  /** Schools worth adding next: ranked by how far they are from the *farthest*
   *  already-picked school, because a candidate only widens a usable
   *  intersection if it is close to all of them, not just to one. */
  function schoolsNearPicks(schools, limit = 8) {
    const out = [];
    for (const cand of state.schools) {
      if (isPicked(cand) || cand.lat == null || cand.lng == null) continue;
      const dists = schools.map((s) => distanceM(s.lat, s.lng, cand.lat, cand.lng));
      const worst = Math.max(...dists);
      // Beyond two outer bands apart there is no overlap left to find.
      if (worst > OUTER_BAND * 2) continue;
      out.push({ school: cand, worst, dists });
    }
    return out.sort((a, b) => a.worst - b.worst).slice(0, limit);
  }

  function renderSchoolPanel() {
    const picks = state.schoolPicks;
    if (!picks.length) return;

    const multi = picks.length > 1;
    const scope = state.schoolScope;
    const near = propertiesNearSchools(picks);
    const nearby = schoolsNearPicks(picks);
    const shown = visibleProperties().length;

    // The index badge must not run into the number: "1" next to "368 m" reads
    // as 1368 m. It gets its own boxed element and a gap, and each school sits
    // on its own line, so the digit can only be read as a label.
    const distCell = (n) => picks.map((s, i) => {
      const d = n.dists[i];
      const cls = d <= P1_BANDS[0] ? "sp-d sp-d--1km" : "sp-d";
      return `<span class="${cls}" title="${escapeHtml(s.name)}">${
        multi ? `<i class="sp-d-n" aria-hidden="true">${i + 1}</i>` : ""
      }<span class="sp-d-v">${fmtDist(d)}</span></span>`;
    }).join("");

    const rows = near.map((n) => `<tr>
        <td>${escapeHtml(n.prop.name)}
          <span class="sp-model">${escapeHtml(
            [n.prop.address && n.prop.address !== n.prop.name ? n.prop.address : "",
             n.prop.model].filter(Boolean).join(" · "))}</span></td>
        <td class="sp-dists">${distCell(n)}</td>
        <td class="num">${psfText(medianPsf(n.txns))}</td>
        <td class="num">${n.prop.top_year || "—"}</td>
      </tr>`).join("");

    el.panelBody.innerHTML = `
      <p class="p-eyebrow"><i class="sk sk--legend" aria-hidden="true"></i>${
        multi ? `${picks.length} primary schools` : "Primary school"}</p>
      <h2 class="p-name">${multi ? "School catchment overlap"
                                 : escapeHtml(picks[0].name)}</h2>
      <p class="p-sub">${multi
        ? "Properties measured against every school below"
        : `${escapeHtml(picks[0].address)} · S(${escapeHtml(picks[0].postal)})`}</p>

      <ol class="sp-picks">${picks.map((s, i) => `<li>
        <span class="sp-pick-n">${i + 1}</span>
        <span class="sp-pick-name">${escapeHtml(s.name)}</span>
        <button type="button" class="sp-pick-x" data-unpick="${escapeHtml(s.postal)}"
                aria-label="Remove ${escapeHtml(s.name)}">&times;</button>
      </li>`).join("")}</ol>

      <div class="p-hero">
        <span class="p-hero-value">${near.length}</span>
        <span class="p-hero-unit">within 2 km${
          multi ? (scope === "all" ? " of all" : " of any") : ""}</span>
      </div>
      <p class="p-hero-label">
        Of the ${num.format(shown)} watched propert${shown === 1 ? "y" : "ies"}
        currently shown${near.filter((n) => n.within1 === picks.length).length
          ? ` · ${near.filter((n) => n.within1 === picks.length).length} inside 1 km${
              multi ? " of every school" : ""}`
          : ""}
      </p>

      ${multi ? `<div class="chips sp-scope" role="group" aria-label="Catchment scope">
        <button type="button" class="chip${scope === "all" ? " is-on" : ""}"
                data-scope="all" aria-pressed="${scope === "all"}">Near all</button>
        <button type="button" class="chip${scope === "any" ? " is-on" : ""}"
                data-scope="any" aria-pressed="${scope === "any"}">Near any</button>
      </div>` : ""}

      <h3 class="p-h3">Properties${multi ? " in the overlap" : " within 2 km"}</h3>
      <p class="p-h3-sub">${multi
        ? `Sorted by the ${scope === "all" ? "farthest" : "nearest"} of the ${picks.length} schools`
        : "Sorted by distance"} · bold = inside 1 km</p>
      ${near.length ? `<div class="p-table-wrap"><table class="p-table sp-table">
        <thead><tr><th>Project / address</th><th>Distance</th>
          <th class="num">Median psf</th><th class="num">TOP</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
        : `<p class="p-empty">No watched property is within 2 km of ${
            multi && scope === "all" ? "all of these schools" : "this selection"}.${
            multi && scope === "all" ? " Try “Near any”, or drop a school." : ""}</p>`}

      <h3 class="p-h3" style="margin-top:22px">Add another school</h3>
      <p class="p-h3-sub">Nearest to ${multi ? "all picks" : "this school"}${
        multi ? ", by the farthest of them" : ""} — click to add</p>
      ${nearby.length ? `<ul class="sp-nearby">${nearby.map((n) => `<li>
          <button type="button" data-pick="${escapeHtml(n.school.postal)}">
            <span class="sp-nb-name">${escapeHtml(n.school.name)}</span>
            <span class="sp-nb-d">${fmtDist(n.worst)}</span>
          </button></li>`).join("")}</ul>`
        : `<p class="p-empty">No other school is close enough to overlap.</p>`}

      <p class="sp-note">Straight-line distance from each school's registered
        postal code to each block's geocoded position — MOE measures the same
        way, but treat anything near the 1 km boundary as indicative and check
        MOE's own tool before relying on it. Being inside 1 km is a
        registration priority, not a guarantee of a place.</p>
    `;

    for (const btn of el.panelBody.querySelectorAll("[data-unpick]")) {
      btn.addEventListener("click", () => {
        const s = picks.find((p) => p.postal === btn.dataset.unpick);
        if (s) selectSchool(s);           // toggling off, same code path
      });
    }
    for (const btn of el.panelBody.querySelectorAll("[data-pick]")) {
      btn.addEventListener("click", () => {
        const s = state.schools.find((p) => p.postal === btn.dataset.pick);
        if (s) selectSchool(s);
      });
    }
    for (const btn of el.panelBody.querySelectorAll("[data-scope]")) {
      btn.addEventListener("click", () => {
        state.schoolScope = btn.dataset.scope;
        renderSchoolPanel();
      });
    }

    el.panel.hidden = false;
    el.scrim.hidden = false;
    if (state.chart) { state.chart.destroy(); state.chart = null; }
  }

  const fmtDist = (m) =>
    m < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(2)} km`;

  // ── panel ─────────────────────────────────────────────────────────────

  function selectProperty(id) {
    state.schoolPicks = [];           // the panel shows one thing at a time
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
    if (state.schoolPicks.length) {
      state.schoolPicks = [];
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
      ${phaseHtml(phase(txns))}
      ${yieldHtml(rentalYield(prop, txns), prop)}
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

  /** The phase verdict. The rate is shown in every case, including "Peaked" —
   *  a reader who disagrees with the classification can see the number it was
   *  drawn from, and the wording never implies the trend will continue. */
  function phaseHtml(p) {
    if (!p) {
      return `<div class="p-phase p-phase--empty">
        <span class="p-phase-tag">Price phase</span>
        Needs at least ${PHASE_MIN_MONTHS} months of sales spanning
        ${PHASE_MIN_SPAN_YEARS} years to classify. Widen the period.
      </div>`;
    }
    const note = p.significant
      ? `Trend of ${pct(p.annual)} a year is clear of the noise in this history`
      : `Fitted trend is ${pct(p.annual)} a year, but the scatter is wide enough
         that flat is just as consistent with the data`;

    return `<div class="p-phase is-${p.key}">
      <div class="p-phase-head">
        <span class="p-phase-tag">Price phase</span>
        <span class="p-phase-label">${p.label}</span>
      </div>
      <p class="p-phase-note">${note} ·
        ${p.months} months over ${p.years.toFixed(1)} yrs ·
        fit R²&nbsp;${p.r2.toFixed(2)}</p>
      <p class="p-phase-caveat">Describes prices already transacted, not a forecast.</p>
    </div>`;
  }

  function yieldHtml(y, prop) {
    if (!y) {
      const why = (prop.rents || []).length
        ? "No rental contracts overlap this period."
        : prop.source === "HDB"
          ? "No approved rentals published for this block and flat type."
          : "URA publishes a median only where a project had enough rental contracts.";
      return `<div class="p-yield p-yield--empty">
        <span class="p-yield-tag">Gross rental yield</span> ${why}</div>`;
    }
    // URA publishes a median per quarter and not the number of leases behind
    // it, so only HDB can honestly quote a contract count. Reporting the
    // quarter count as "contracts" would overstate what is known.
    const depth = prop.source === "HDB"
      ? (y.contracts ? ` · ${num.format(y.contracts)} contracts` : "")
      : ` · ${y.months} quarterly medians`;
    const src = prop.source === "HDB"
      ? `HDB approved rentals, converted to psf using this block's median floor area`
      : `URA median rent for the project`;
    return `<div class="p-yield">
      <div class="p-yield-head">
        <span class="p-yield-tag">Gross rental yield</span>
        <span class="p-yield-value">${(y.value * 100).toFixed(2)}%</span>
      </div>
      <p class="p-yield-note">
        ${y.rentPsf.toFixed(2)} psf/month × 12 ÷ ${psfText(y.psf)} ·
        ${src} · ${monthLabel(y.firstDate)} – ${monthLabel(y.lastDate)}${depth}
      </p>
      <p class="p-yield-caveat">Before maintenance, tax, agent fees and vacancy.${
        y.matched ? "" : " Rent taken from the latest published quarters — " +
          "the selected price period predates published rental data."}</p>
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
                const g = growthAt(base, raw, labels[0], labels[item.dataIndex]);
                return [
                  `${pct(g.cum)} since ${monthLabel(labels[0])}`,
                  g.annual != null
                    ? `${pct(g.annual)} a year (CAGR)`
                    : `under a year — no annual rate yet`,
                  `${psfText(raw)} psf`,
                ];
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

  /** Where the period starts on load and after Reset. `presetStart` returns
   *  null when the history is shorter than the preset, so a young dataset
   *  opens on everything it has rather than on nothing. */
  function defaultStartIdx() {
    const preset = PERIOD_PRESETS.find((p) => p.id === DEFAULT_PERIOD);
    const start = preset ? presetStart(preset) : null;
    return start == null ? 0 : start;
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

  const LEASE_BUCKET = 5;   // years per bar

  /** Histogram of lease remaining, drawn on the slider's own 0..max scale so
   *  each bar sits above the position that selects it.
   *
   *  Counts ignore the lease filter (see passesProperty) — the distribution
   *  has to stay still while you drag, or the shape you are aiming at moves
   *  as you approach it. Dragging only changes which bars read as kept. */
  function renderLeaseHistogram() {
    if (!el.leaseHist) return;
    const max = state.leaseMax || 99;
    const bars = Math.max(1, Math.ceil(max / LEASE_BUCKET));
    const counts = new Array(bars).fill(0);
    let noLease = 0;

    for (const p of state.properties) {
      if (!passesProperty(p, "lease")) continue;
      if (!matchingTxns(p).length) continue;
      const left = yearsLeft(p);
      if (left == null) { noLease++; continue; }   // freehold / unknown
      counts[Math.min(bars - 1, Math.floor(left / LEASE_BUCKET))]++;
    }

    if (el.leaseFh) {
      el.leaseFh.hidden = noLease === 0;
      el.leaseFh.textContent = `+${noLease} freehold`;
      el.leaseFh.title =
        `${noLease} freehold propert${noLease === 1 ? "y is" : "ies are"} not on this `
        + "scale — they have no lease to run down, and pass any minimum";
    }

    const peak = Math.max(...counts);
    if (!peak) {
      el.leaseHist.innerHTML = "";
      el.leaseHist.setAttribute("aria-label", "No properties to show a lease distribution for");
      return;
    }

    const w = 100 / bars;
    el.leaseHist.innerHTML =
      `<svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">` +
      counts.map((n, i) => {
        const lo = i * LEASE_BUCKET;
        const h = n ? Math.max(3, (n / peak) * 100) : 0;
        // A bar is "kept" when its whole bucket clears the minimum.
        const kept = lo + LEASE_BUCKET > state.minLease;
        return n === 0 ? "" : `<rect class="lh-bar${kept ? "" : " is-out"}"
          x="${(i * w + 0.35).toFixed(2)}" width="${(w - 0.7).toFixed(2)}"
          y="${(100 - h).toFixed(2)}" height="${h.toFixed(2)}"
          data-min="${lo}"><title>${lo}–${lo + LEASE_BUCKET - 1} years: ${n} propert${
            n === 1 ? "y" : "ies"}</title></rect>`;
      }).join("") + `</svg>`;

    const total = counts.reduce((a, b) => a + b, 0);
    el.leaseHist.setAttribute("aria-label",
      `Lease remaining across ${total} propert${total === 1 ? "y" : "ies"}, `
      + `${LEASE_BUCKET}-year bands from 0 to ${max}`
      + (noLease ? `, plus ${noLease} freehold or unknown` : ""));

    // Clicking a bar sets the minimum to that band — the histogram is the
    // thing you're aiming at, so let it be the target.
    for (const rect of el.leaseHist.querySelectorAll(".lh-bar")) {
      rect.addEventListener("click", () => {
        state.minLease = +rect.dataset.min;
        el.minLease.value = String(state.minLease);
        syncLeaseUI();
        applyFilters();
      });
    }
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
    renderLeaseHistogram();
    syncFilterBadge();
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
    // The catchment table is built from visibleProperties() and their median
    // psf, so it goes stale on every filter and period change exactly like the
    // other two. Every view that reads the filters must be refreshed here —
    // this list has been the source of the same bug twice already.
    if (state.schoolPicks.length) renderSchoolPanel();
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
      if (!passesProperty(p, "model")) continue;
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
    syncFilterBadge();
    updateModelCounts();
    renderLeaseHistogram();
    renderMarkers({ fit: true });
    // This is the one filter path that doesn't go through applyFilters, and
    // Reset ends here — without this the open detail views keep showing the
    // numbers from before the reset.
    refreshDetailViews();
  }

  function resetView() {
    // Back to how the page opens, which is the default period — not the full
    // range. "Reset" means "as I found it".
    state.startIdx = defaultStartIdx();
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
    state.startIdx = defaultStartIdx();
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
    renderLeaseHistogram();
    buildPresets();
    syncRangeUI();
    syncPresets();
    syncLeaseUI();
    syncFilterBadge();
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
    el.landToggle.addEventListener("click", toggleLand);
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
      // Parcel colours were baked into the canvas at draw time, and dark mode
      // has its own steps rather than a filtered version of the light ones.
      renderLand();
      renderLandLegend();
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

    // HEAD, not GET: this only decides whether the button is worth showing,
    // and the file itself is ~700 KB that most visits never open.
    try {
      const res = await fetch("masterplan.json", { method: "HEAD" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.warn("no masterplan.json — land-use layer disabled", err);
      el.landToggle.hidden = true;
    }
  }

  boot();
})();
