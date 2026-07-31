# Singapore Property Tracker — build plan & handover

**Purpose of this file.** A complete record of what was built, in what order, and
*why* the non-obvious decisions were made. Read it before changing anything.

Most of it is reasoning that cannot be recovered by reading the code: API
quirks discovered the hard way, design rules being deliberately held, and bugs
that recurred because a rule was applied in one place and not another. The code
tells you *what*; this tells you *why*, and which mistakes have already been
made once.

Last updated: 2026-07-30. Live at <https://andrewchengyr.github.io/property-tracker/>

---

## 1. What this is

A personal tool tracking **transacted** (actually sold) prices for a
user-editable watchlist of Singapore properties — HDB resale flats and private
condos — accumulating history over time and rendering it on an interactive map
that works on desktop and phone.

Repo-native and free. No server, no paid services. GitHub Actions refreshes
weekly; GitHub Pages serves the map. The repo is the single source of truth.

```
config/watchlist.yaml
      │
      ▼
ingest/ ──► URA Data Service (private caveats)
        ──► data.gov.sg (HDB resale, MOE school directory)
        ──► OneMap (geocoding, planning-area polygons)
      │
      ▼
data/transactions.db (SQLite, COMMITTED)
      │
      ▼
web/data.json + web/schools.json ──► Leaflet + Chart.js on GitHub Pages
      ▲
GitHub Actions (weekly cron) ─┘
```

**Current state:** 410 properties, 7,699 transactions (Jan 2017 – Jul 2026),
179 primary schools, 691 cached geocodes. 90 tests, all offline. 46 commits
(22 of them automated refreshes).

---

## 2. Ground rules being held

These are decisions, not accidents. Changing one is fine — knowing you're
changing it is the point.

1. **The database is committed and never deleted from.** `ingest/store.py` has
   no `DELETE`. This is the whole answer to URA's rolling 5-year window (§5.1).
   Do not gitignore `data/transactions.db`, do not "clean it up", and never let
   a fresh empty one get committed over it.
2. **Every source degrades, none crashes the run.** A failed source logs, is
   collected into `errors`, and the run exits non-zero *after* still exporting
   and deploying. The site keeps serving the last good data while CI goes red.
   This has been violated twice and both times it took down unrelated sources.
3. **Dedup keys contain no derived fields.** Coordinates are filled in *after*
   a row is first stored; keying on them broke idempotency (§5.3).
4. **Colour follows the entity, never its rank.** A filter or a removal must
   not repaint the survivors.
5. **Facet counts skip their own filter.** A model chip's count and the lease
   histogram each ignore the filter they drive — otherwise selecting a value
   zeroes every other option and there is nothing left to navigate by.
   `passesProperty(p, skip)` in `web/app.js` is the single implementation.
6. **Validate colour, don't eyeball it.** Every palette here was run through
   the `dataviz` skill's `scripts/validate_palette.js`. Two were rejected on
   measurement (§6).
7. **Verify against reality, not just against the code.** Several bugs looked
   fine in code review and were caught only by checking output against known
   ground truth (§5.2, §5.6).

---

## 3. Milestones, in order

Spec milestones M1–M6 came from the original brief
(`~/Downloads/property-tracker-spec.md`); everything after M6 was requested
conversationally.

| # | Milestone | Commit | Notes |
|---|---|---|---|
| M1 | Scaffold | `49c5de8` | Repo layout, watchlist, `.env.example` |
| M2 | Ingestion offline | `49c5de8` | URA + HDB clients, SQLite store, 36 tests against fixtures |
| M3 | Live pull | — | Real credentials; exposed §5.1–5.3 |
| M4 | Frontend map | `49c5de8` | Leaflet + Chart.js, psf colouring, time slider |
| M5 | Pages deploy | `d7b0368` | Live URL |
| M6 | Weekly cron | `acdabc8` | Plus fail-loudly hardening |
| — | Watchlist growth | `0b71608`→`aef01e1` | Toa Payoh, then Bishan, HDB + private |
| — | Model filter | `c4622bb`, `d41d1d6` | Chips built from data; counts made live |
| — | Schools + P1 rings | `32e01ef` | 179 MOE primary schools, 1 km / 2 km bands |
| — | Period presets | `4f7d153` | YTD…10Y, anchored to newest data month |
| — | Planning-area selection | `98a1f6a` | "All condos in Toa Payoh and Bishan" |
| — | Compare | `d205cb0`→`24301a1` | 3-way side-by-side, growth mode, CAGR tooltips |
| — | Lease histogram | `7d07795` | Distribution above the lease slider |

---

## 4. Architecture

### 4.1 Ingestion (`ingest/`)

| Module | Responsibility |
|---|---|
| `run.py` | Entrypoint. Watchlist loading, source orchestration, error collection |
| `models.py` | `Transaction` dataclass, dedup key, tenure parsing (`lease_facts`) |
| `ura.py` | URA Data Service: daily token (cached), 4 batches, name matching |
| `hdb.py` | data.gov.sg HDB resale, street-abbreviation canonicalisation |
| `schools.py` | MOE school directory, filtered to PRIMARY |
| `planning.py` | OneMap planning-area polygons + point-in-polygon |
| `geocode.py` | OneMap token/search, SVY21→WGS84, cache-first `Geocoder` |
| `datagov.py` | Shared retrying client for **both** data.gov.sg callers |
| `store.py` | SQLite schema, migrations, idempotent upsert, JSON/CSV export |

**Run it:**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m ingest.run --from-fixtures   # offline, no keys
.venv/bin/python -m ingest.run                   # live
```

Flags: `--skip-ura`, `--skip-hdb`, `--skip-schools`, `--from-fixtures`,
`--no-csv`, `-v`.

**Credentials** (`.env`, gitignored; also GitHub Actions secrets):
`URA_ACCESS_KEY`, `ONEMAP_EMAIL`, `ONEMAP_PASSWORD`. data.gov.sg needs none.
Audited: no secret has ever been committed. The OneMap *email* does appear as
the git author address, which is normal but worth knowing.

### 4.2 Frontend (`web/`)

Buildless — `index.html`, `app.js`, `style.css`, CDN Leaflet + Chart.js.

**Bump `?v=N` on both asset links whenever `app.js` or `style.css` changes.**
Currently `v=27`. Without it a browser or CDN edge serves a stale script
against fresh markup, which fails confusingly rather than cleanly — this cost
real debugging time twice.

Encoding: marker **colour** = median psf (sequential, fixed scale); marker
**shape** = source (circle private, square HDB). Two channels, two jobs, so
filtering changes a marker's value without changing what its shape means.

### 4.3 CI (`.github/workflows/refresh.yml`)

Weekly `0 20 * * 0` (Mon 04:00 SGT) + `workflow_dispatch`. Pulls, commits the
db and exports, publishes `web/` as a Pages artifact (Pages can only serve `/`
or `/docs` from a branch, hence the artifact). The ingest step is
`continue-on-error` so the site deploys from last-good data, and a final step
fails the run if a source errored.

**The bot commits every week even when nothing changed** — `data.json` carries
a `generated_at` timestamp. So `git pull` before local work.

---

## 5. Discovered API behaviour — read before touching ingestion

Every item here was found by checking real output. None is in the spec.

### 5.1 URA serves a rolling 5-year window
Documented behaviour, not a bug. Private transactions start ~5 years before
today and that boundary slides forward weekly. URA advises keeping only 5 years
because older caveats get **revised or voided**.

The committed database is the mitigation: it already holds July-2021 rows that
URA no longer serves. Consequence to accept: we cannot distinguish "aged out"
from "voided", so deep history is "best known at the time", not audited.
Test: `test_rows_survive_falling_out_of_uras_5_year_window`.

### 5.2 URA returns **no** `x`/`y` coordinates
The spec says caveats carry SVY21 coordinates; the live feed carries none, so
every private project is geocoded through OneMap like HDB. **OneMap credentials
are therefore required for private property too.** The SVY21→WGS84 conversion
is still wired up for if they return.

### 5.3 Dedup keys must exclude coordinates
Because of 5.2, coordinates arrive *after* the first store. Keying on them made
every second run re-insert instead of update (+131 duplicate rows). Identity is
the transaction. Test: `test_rows_that_gain_coordinates_later_do_not_duplicate`.

### 5.4 The HDB dataset abbreviates street names
`LOR 1A TOA PAYOH`, not `LORONG 1A TOA PAYOH`. A watchlist entry with the full
word matched **zero** records, silently. `STREET_ABBREV` in `hdb.py`
canonicalises both sides. Add missing abbreviations there.

### 5.5 "Executive maisonette" is not a flat type
`flat_type` is `EXECUTIVE`; maisonette vs apartment lives in `flat_model`.
Querying `EXECUTIVE MAISONETTE` returns zero rows.

### 5.6 Substring matching is wrong for area-selected names
Planning-area selection yields exact API names. Matching them as substrings
pulled **THE ORIENT** (Pasir Panjang) in behind **THE ORIE** (Lorong 1 Toa
Payoh) — a project from the other side of the island. `ura.normalize()` takes
`exact_names` separately from substring `wanted_projects`; hand-typed watchlist
entries still match loosely so `"trevis"` finds Trevista.

### 5.7 One block can hold several flat types *and* models
8 Joo Seng Rd has 5 ROOM and EXECUTIVE; 236 Lor 1 Toa Payoh and 254 Kim Keat
Ave each hold executive maisonettes *and* apartments (~166 vs ~142 sqm).
Export groups on **name + type + model**; grouping on name alone produced one
marker with a blended psf under whichever label sorted last.

### 5.8 Both external APIs throttle aggressively
- **URA token minting** → 403 on rapid re-mints. Retries with backoff; the
  daily token is cached to `.ura_token.json`.
- **URA token expiry is reported as HTTP 200** with the failure in the body
  (`"Token is valid for one day only…"`). A status-code check alone silently
  loses all four batches. Minting also invalidates the previous token, so a
  cached token dies whenever CI mints with the same key.
- **data.gov.sg** → 429 when a watchlist makes several calls in a row.
  `datagov.py` is shared by the HDB *and* schools clients precisely because
  hardening one and not the other broke a scheduled run.
- **OneMap token minting** → 400/429 under load. Retries; and a failure must
  never propagate (it once crashed the whole run including HDB).

### 5.9 HDB towns and URA planning areas are administrative, not geographic
`TOA PAYOH` includes Potong Pasir, Bidadari, Joo Seng and Kim Keat. `BISHAN`
includes Marymount, Sin Ming, Shunfu and Upper Thomson. Postal districts are
wider still (D12 = Balestier + Toa Payoh + Serangoon), which is why
planning-area selection uses **point-in-polygon**, with `districts` only as a
pre-filter to bound how many projects need geocoding.

### 5.10 35 of 67 private projects are Freehold
Correctly parsed, not a parser failure. They have no years remaining, so they
cannot sit on the lease histogram's axis and are counted beside it
(`+35 freehold`). They pass *any* lease minimum, so the further right that
slider goes, the higher the freehold share of what remains.

---

## 6. Colour decisions (all measured)

Run from the `dataviz` skill directory:
`node scripts/validate_palette.js "<hex,hex>" --mode light|dark [--pairs all]`

| Use | Colours | Why |
|---|---|---|
| psf ramp | blue 5-step, `#86b6ef`→`#104281` | Sequential, one hue, light→dark. Passes both modes |
| Schools | orange `#eb6834` / `#d95926` | **Violet was rejected**: ΔE 1.9 vs mid-blue under protanopia, 9.0 even with normal vision — school pins read as expensive properties. Orange is warm/cool opposite, worst pair ΔE 23.1 |
| Compare series | blue / orange / aqua (slots 1-3) | **Blue/aqua/magenta was rejected**: ΔE 1.6 dark mode. These pass all-pairs both modes (9.2 light / 9.4 dark) |

Compare slot 2 shares orange with the school layer — which is why compared
markers also carry a **slot number**, so identity never rests on colour alone.

---

## 7. Frontend behaviour worth not breaking

- **Filters.** Type and lease are *property*-level; period, size and price are
  *transaction*-level (a property vanishes only when none of its transactions
  qualify). Empty means unbounded — `0` is a real bound, so `Number(v) || null`
  would be wrong.
- **`refreshDetailViews()`** is called by *both* filter paths (`applyFilters`
  and `setSource`, where Reset ends). Reset left a stale detail view **twice**
  because one path refreshed the drawer and forgot the panel. Add new detail
  views there, not to one caller.
- **Compare**: number follows *position* (leftmost is #1), colour follows the
  *property* (stable until removed). Reorder by drag or ‹ › buttons.
- **Growth mode** rebases each line to its own first month in the selected
  period. Points under a year from the base show no annual rate — annualising
  four months extrapolates it to a year it hasn't lived through. `growthAt()`
  is shared with the headline figure so the last point equals it by
  construction.
- **`[hidden] { display: none !important }`** is deliberate: any author rule
  setting `display` silently beats the UA sheet's `[hidden]`, which left a
  hidden legend entry and a hidden button visible.
- **Map fit** waits for the map container's height to be stable across two
  frames. Fitting against a transient height (data.json resolves before the
  CDN stylesheet) lands a zoom level or two too far out.

---

## 8. Testing

`.venv/bin/python -m pytest tests/ -q` — **90 tests, ~0.2s, all offline.**
No test touches the network; fixtures live in `tests/fixtures/`.

Fixture values are **synthetic** but field names and shapes mirror the real
APIs exactly, and each file deliberately contains records the filters should
*reject*. See `tests/fixtures/README.md`.

Regression tests exist for every §5 item that bit once. Keep it that way.

---

## 9. Open items

**Answered but not built:**

- **URA Master Plan 2025 overlay — feasible.** Free GeoJSON on data.gov.sg
  (`d_a8c3546b26712e35021f3a681d0353ae`), gazetted 1 Dec 2025. Full file is
  **181 MB / 113,394 parcels**; clipped to the Toa Payoh + Bishan planning
  areas (which `planning.py` already holds) it is **6,362 parcels, 6.8 MB →
  2.0 MB gzipped**. Parcels carry `LU_DESC` and `GPR` (plot ratio). Design
  constraint: **22 land-use categories** in those two areas vs a palette that
  tops out at 8 — group into ~6 buckets with the exact `LU_DESC` on hover.
  Lazy-load only when toggled.
- **PropertyGuru asking prices — not for this dashboard.** Behind a Cloudflare
  managed challenge (even `/robots.txt` returns a JS challenge; listing pages
  403). Getting past it means defeating bot detection, which is off the table.
  Separately, the repo and site are **public**, so redistributing their listing
  data is a bigger problem than personal use. A `propertyguru-daily-tracker`
  skill exists for personal, browser-driven, local tracking — keep it out of
  `ingest/`. Also note asking ≠ transacted: listings are aspirational, stale
  and duplicated across agents, and would corrupt every median and CAGR if
  blended in. Legitimate route at scale is a commercial data licence.

**Available, not requested:**

- **HDB history back to 1990.** Four older free datasets, ~746k records, same
  fields: `d_ebc5ab87086db484f88045b47411ebc5` (1990-1999),
  `d_43f493c6c50d54243cc1eab0df142d6a` (2000-Feb 2012),
  `d_2d5ff9ea31397b66239f245f57751537` (Mar 2012-Dec 2014),
  `d_ea9ed51da2787afaf8e51f827c304208` (Jan 2015-Dec 2016). Would take HDB
  from 9 years to 36. Very old rows may lack `remaining_lease`, but
  `lease_commence_date` — which the lease maths uses — is present.
- **`--forget` flag.** Removing a watchlist entry stops *new* rows arriving but
  leaves existing ones on the map, because the export reads every row. There is
  no way to genuinely remove a property short of editing the database.
- **Growth toggle on the hover card.** Only the panel and compare charts have it.
- **District → area names.** URA properties show a numeric district (`12`,
  `20`) where HDB shows a town name.

---

## 10. Working agreements

Carried over from the build; a new session should keep to them.

- Check output against reality before declaring something works — several bugs
  passed code review and failed only against ground truth.
- State bad news plainly and early: what broke, what it means, what's needed.
- Prefer fixing the *class* of bug over the instance. Reset broke twice and
  data.gov.sg throttling broke twice, both because the first fix was applied to
  one caller.
- Commit messages explain *why*, including what was rejected and on what
  measurement.
- Don't commit or push unless asked. Don't add watchlist entries the user
  didn't ask for.
