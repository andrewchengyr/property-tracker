# Singapore Property Tracker — build plan & handover

**Purpose of this file.** A complete record of what was built, in what order, and
*why* the non-obvious decisions were made. Read it before changing anything.

Most of it is reasoning that cannot be recovered by reading the code: API
quirks discovered the hard way, design rules being deliberately held, and bugs
that recurred because a rule was applied in one place and not another. The code
tells you *what*; this tells you *why*, and which mistakes have already been
made once.

Last updated: 2026-07-31. Live at <https://andrewchengyr.github.io/property-tracker/>

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
        ──► data.gov.sg (HDB resale, MOE school directory, Master Plan 2025)
        ──► OneMap (geocoding, planning-area polygons)
      │
      ▼
data/transactions.db (SQLite, COMMITTED)
      │
      ▼
web/data.json + web/schools.json ──► Leaflet + Chart.js on GitHub Pages
       + web/masterplan.json        ▲
GitHub Actions (weekly cron) ───────┘
```

**Current state:** 904 properties, 12,107 transactions (Jan 2017 – Jul 2026),
179 primary schools, 8,695 land-use parcels, 1,121 cached geocodes. 118 tests,
all offline. 50 commits (24 of them automated refreshes).

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
6. **Validate colour, don't eyeball it — and validate what is actually
   rendered.** Every palette here was run through the `dataviz` skill's
   `scripts/validate_palette.js`. Three were rejected on measurement (§6). For
   translucent marks that means grading the *composited* colour against the
   surface behind it, not the solid hex: doing that is what killed the pale
   land-use wash the overlay was first designed around (§6.1).
7. **Verify against reality, not just against the code.** Several bugs looked
   fine in code review and were caught only by checking output against known
   ground truth (§5.2, §5.6) or by looking at the rendered map (§5.11).

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
| — | Master Plan overlay | — | MP2025 land use, clipped to the watched areas; §5.11, §6, §11 |
| — | Rental yield | — | New `ingest/rental.py` + `rentals` table; §5.12, §12.1 |
| — | Price phase | — | Momentum / Peaked / Cooling from a log-linear fit; §12.2 |
| — | Compare Schools | — | Multi-school catchment intersection; §12.3 |
| — | Central Region HDB | — | 7 towns added; exposed §5.13 and §5.14 |
| — | P1 balloting | — | MOE vacancies/balloting per school; §5.15, §5.16, §12.4 |

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
| `masterplan.py` | URA Master Plan 2025 land use: download, clip, bucket, shrink |
| `store.py` | SQLite schema, migrations, idempotent upsert, JSON/CSV export |

**Run it:**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m ingest.run --from-fixtures   # offline, no keys
.venv/bin/python -m ingest.run                   # live
```

Flags: `--skip-ura`, `--skip-hdb`, `--skip-schools`, `--refresh-masterplan`,
`--from-fixtures`, `--no-csv`, `-v`.

**Credentials** (`.env`, gitignored; also GitHub Actions secrets):
`URA_ACCESS_KEY`, `ONEMAP_EMAIL`, `ONEMAP_PASSWORD`. data.gov.sg needs none.
Audited: no secret has ever been committed. The OneMap *email* does appear as
the git author address, which is normal but worth knowing.

### 4.2 Frontend (`web/`)

Buildless — `index.html`, `app.js`, `style.css`, CDN Leaflet + Chart.js.

**Bump `?v=N` on both asset links whenever `app.js` or `style.css` changes.**
Currently `v=29`. Without it a browser or CDN edge serves a stale script
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

### 5.11 The Master Plan is a *plan*, and one parcel can be 8 km wide

Four things about `d_a8c3546b26712e35021f3a681d0353ae`, all found by looking at
real output:

- **No `Description` HTML blob.** Many data.gov.sg geo layers bury their
  attributes in an HTML table inside a `Description` property. This one does
  not: `LU_DESC` and `GPR` are flat properties, and coordinates are already
  WGS84, so no SVY21 conversion is needed. Ten properties per parcel arrive;
  three survive the export.
- **`GPR` is not always a number.** Island-wide it is `LND` (landed housing,
  59,055), `EVA` (subject to evaluation, 12,891), `SDP` (subject to detailed
  planning, 329), null (7,946), or a ratio from 0.6 to 25.0. `GPR_CODES` in
  `masterplan.py` expands the three codes so the meaning travels with the data
  rather than living in the frontend.
- **A "parcel" can be the whole Central Catchment.** This is the one that bit.
  Clipping was first written as "keep the parcel if *any* vertex falls inside
  the planning area", reasoning that a parcel straddling the boundary should
  stay rather than fray the overlay's edge. That is right for the ~230 m²
  median parcel and catastrophic for the outlier: the catchment is a **single**
  OPEN SPACE polygon **8.1 km across**, it touches Bishan, and so the whole of
  it — Bukit Panjang to Thomson — was painted onto a map that claims to cover
  two planning areas. Parcels are now assigned by a **representative point**
  (largest ring's centroid, pulled back onto the ring when the shape is concave
  enough to expel it), so each parcel belongs to exactly one area. Cost: a
  parcel whose centre is just outside but which pokes in is dropped, leaving at
  most a lot's width of gap at the boundary. Invisible, and unlike the other
  error it cannot put a different part of the island on the map.
  Tests: `test_a_parcel_that_merely_touches_the_area_is_not_dragged_in`,
  `test_representative_point_stays_on_a_concave_parcel`.
- **Verified against known ground truth** before any of it was drawn: Trevista
  sits on RESIDENTIAL GPR 4.2, Sky Vue and Sky Habitat on 4.9, and the watched
  HDB blocks on 2.8–3.0. Plausible for high-rise-near-MRT and for HDB
  respectively, which is what made the rest of the numbers trustworthy.

Scale: 181 MB and 113,394 parcels island-wide → **8,695 parcels, 5.7 MB, 1.13
MB gzipped** clipped to Toa Payoh + Bishan + Tampines, with coordinates at 6
decimal places (~0.11 m) and the unused properties dropped. Toa Payoh + Bishan
alone were 6,361 parcels / 623 KB gzipped; the earlier estimate in §9 was 6,362
parcels / 6.8 MB, so the parcel count matched and the size difference is the
trimming. Each further planning area costs roughly another 500 KB gzipped, on a
layer that is only fetched when someone opens it.

**Refreshing is opt-in (`--refresh-masterplan`), not weekly.** MP2025 was
gazetted 1 Dec 2025 and the previous edition ran from 2019 — a roughly
five-yearly cycle. A weekly rebuild would spend the 181 MB download *and*
commit a 3.3 MB diff to reproduce a file that hadn't changed. The run does
build it automatically when the export is missing, so a fresh clone doesn't
need to know the flag.

### 5.12 The HDB rental dataset spells flat types differently

`Renting Out of Flats` (`d_c9f57187485a850908655db0e8cfe651`) writes
**`5-ROOM`** with a hyphen; the resale dataset writes **`5 ROOM`** with a
space. Joining the two on the raw string matches **zero** rows and reports no
error — the same failure mode as the street abbreviations in §5.4, found the
same way (checking a known block's output, not reading the code).
`rental.canonical_flat_type` normalises it, and a test asserts the fixture
still *contains* the hyphen form so the guard can't quietly become a no-op.

**Coverage is partial by design, in both sources.**

- URA `PMI_Resi_Rental_Median` publishes a project's median only where enough
  contracts closed that quarter: **39 of our 67 private projects** are covered.
  It runs 2023Q3–2026Q2 — **three years, quarterly**, not the five the sale
  caveats span.
- HDB covers **683 of 839** blocks, monthly, from 2021-01.

So ~20% of the map has no yield and never will. The panel says which of the
two reasons applies rather than showing a blank or a zero.

**URA's `median` is already psf per month.** No area conversion is applied to
it, and a test pins the plausible range so nobody later "fixes" it by dividing
by a floor area. HDB publishes rent *per unit* with no area at all, so its psf
is derived from the block's own median resale floor area — approximate within
a flat type, and the only way HDB yields exist at all.

### 5.13 The two HDB datasets disagree on names — assume nothing, enumerate

Resale and "Renting Out of Flats" describe the same flats and spell three
fields differently. Each one was found the same way: a join returned nothing,
silently, and only a coverage number that looked wrong gave it away.

| Field | Resale | Rental | Handled in |
|---|---|---|---|
| Street | `LORONG 1A TOA PAYOH` | (same abbreviations) | `hdb.canonical_street` |
| Flat type | `5 ROOM` | `5-ROOM` | `rental.canonical_flat_type` |
| Town | `CENTRAL AREA` | `CENTRAL` | `rental.canonical_town` |

The town one is the nastiest, because the filter is applied **server-side**: a
wrong town returns an empty `200`, not an error, so Central Area simply had no
yield while every other town sat at 72-100%. `fetch_hdb` now warns on an empty
result for exactly this reason.

**The rule: before joining these datasets on any field, enumerate the distinct
values on both sides.** Both town lists were enumerated to confirm CENTRAL AREA
is the only divergence (the rental set also has TENGAH, which has no resale
stock yet); a test pins the other nine names as shared verbatim, because an
alias added on a guess would silently point a town at nothing.

### 5.14 Rentals must be collected after the upsert, not after collection

`collect_rentals` derives HDB rent-psf from floor areas read out of the
**database** (`median_areas_sqft`). Transactions live in an in-memory list
until `upsert_many` runs, so a rental step placed merely after `collect_hdb`
sees only what previous runs committed.

The failure is invisible on a populated database: every town already committed
still matches, and only towns added *since the last run* come back empty. It
surfaced when seven Central Region towns were added at once and coverage fell
from 80% to 49%. The regression test therefore runs against a **fresh
database** — the only condition that exposes it — and the two HDB fixtures had
to be made to overlap first, because they covered different blocks and so
could never have caught it offline.

### 5.15 `mainlevel_code == "PRIMARY"` misses three P1 schools

Catholic High, CHIJ St. Nicholas Girls' and Maris Stella High are through-train
schools coded **`MIXED LEVEL (P1-S4)`** in the school directory. They run the
P1 registration exercise like any other primary school and are among the most
sought-after in the country — and the exact-string filter left all three off
the map from the day the school layer was built. Found only when their names
turned up on MOE's balloting page with no counterpart on our side.

`schools.takes_p1` now matches "takes P1 students" rather than one literal
code, so a future level string that still starts at P1 is included instead of
silently dropped. 179 → 182 schools.

### 5.16 MOE publishes one P1 exercise and replaces it

The balloting page always shows the most recent exercise; there is no 2024 page
and no year parameter. Archived copies of the older pages exist but are
**shells** — the data used to be fetched client-side and no crawler kept it, so
2023 and 2024 are simply not recoverable. Same shape as URA's rolling five-year
window (§5.1) and the same answer: `p1_ballot` accumulates and is never
deleted, so each year's run adds one permanently.

The data is not an API. It is embedded in the page as a Next.js flight payload
and parsed out of the HTML, which makes it the most fragile source here — a
site rebuild breaks it.

**A 200 does not mean the fetch worked.** The site sits behind CloudFront and
varies on `rsc` and the Next.js router headers, so an edge can hold a variant
of this URL that renders the shell with no `schoolData` in it. The first CI run
got exactly that — a clean 200, no payload — while the identical request from
another network returned the full 728 KB. `fetch` therefore checks for the
payload rather than the status code, retries with a varied cache key and
`Cache-Control: no-cache`, and only then gives up. It therefore degrades like the rest: a failed pull logs
and keeps the archived years, and the export reads from the **database** rather
than from the pull so previously stored years survive a year that cannot be
fetched.

**Fourth name-matching join in this project** (after §5.13's three): MOE's
balloting page appends `(Primary)` to through-train schools and punctuates
differently from the directory (`CHIJ St. Nicholas Girls'` vs `CHIJ ST.
NICHOLAS GIRLS'`). Normalising handles all but one; `St Andrew's Junior School`
vs `ST ANDREW'S SCHOOL (JUNIOR)` needs an explicit alias. Both lists were
enumerated to establish that, per the rule in §5.13.

Three schools on our map have no balloting data at all — Damai, Kranji and
Townsville, which are not taking a P1 intake. They say so rather than showing
an empty table.

---

## 6. Colour decisions (all measured)

Run from the `dataviz` skill directory:
`node scripts/validate_palette.js "<hex,hex>" --mode light|dark [--pairs all]`

| Use | Colours | Why |
|---|---|---|
| psf ramp | blue 5-step, `#86b6ef`→`#104281` | Sequential, one hue, light→dark. Passes both modes |
| Schools | orange `#eb6834` / `#d95926` | **Violet was rejected**: ΔE 1.9 vs mid-blue under protanopia, 9.0 even with normal vision — school pins read as expensive properties. Orange is warm/cool opposite, worst pair ΔE 23.1 |
| Compare series | blue / orange / aqua (slots 1-3) | **Blue/aqua/magenta was rejected**: ΔE 1.6 dark mode. These pass all-pairs both modes (9.2 light / 9.4 dark) |
| Land use | 6 buckets, own hues (§6.1) | Validated all-pairs against the **basemap**, not the page: CVD ΔE 11.0 light / 9.4 dark, normal-vision 19.0 / 17.6 |

Compare slot 2 shares orange with the school layer — which is why compared
markers also carry a **slot number**, so identity never rests on colour alone.

### 6.1 Land use: what the measurement forced

The land-use overlay is the one place where running the validator didn't just
pick between candidate palettes — it **changed the design twice**. Worth
recording, because both conclusions are counter-intuitive and neither is
recoverable from the CSS.

**A map is an all-pairs form.** Any two land uses can abut, so the palette has
to clear the gates on *every* pair, not just adjacent ones. The `dataviz`
skill's own palette certifies only its **first three slots** under `--pairs
all`. Six categories were needed, so the palette had to be purpose-built. An
exhaustive sweep of equally-spaced hue wheels said four was the ceiling (N=5
fell to normal-vision ΔE 13.9, under the hard floor of 15) — but that was an
artefact of forcing one lightness for all slots. With lightness free per slot,
six clears comfortably. Both facts are worth keeping: *equal spacing is the
binding constraint, not the count.*

**Translucent fills are not a readable encoding, and this is measurable.** The
obvious design — pale washes so the basemap shows through — was killed by
grading the *composited* colours (`α·fill + (1−α)·basemap`) rather than the
solid hex, which is what a reader actually sees:

| α | worst all-pairs CVD ΔE | worst normal-vision ΔE | |
|---|---|---|---|
| 1.00 | 11.0 | 19.0 | passes |
| 0.75 | 7.3 | 13.0 | fails |
| 0.60 | 5.1 | 9.7 | fails |
| 0.45 | 3.5 | 7.0 | hopeless |

Compositing over the basemap drags lightness above the band and chroma below
the floor — the validator's phrase is "reads gray", which is exactly what a
pale six-colour wash does. **No hue assignment rescues it at any opacity.** So
the six buckets are drawn near-opaque (0.82).

**Which is why residential is the ground, not a seventh colour.** Near-opaque
fills over 100% of the map would bury the streets. Residential is 82% of
parcels here *and* is what the whole map is about, so it is drawn as a quiet
neutral tint at 0.34 (`--lu-homes`) — deliberately outside the categorical set,
because it is a surface, not a category, and the chroma floor does not apply to
it. The six buckets are the *exceptions*: what is near your flat that isn't
housing. The basemap stays readable across the residential majority, which is
most of the map. This is the rare case where the accessibility constraint and
the editorial one pointed the same way.

**Hues sit in the arcs the map hadn't already spent.** The psf ramp owns blue
(OKLCH h≈253–257) and the school pins own orange (h≈41); both bands are
excluded, so a parcel can never be mistaken for a price or a school. What was
left went to convention where it could: green for parks, purple for industry,
cyan for civic (blue being taken), red-pink for commercial.

**The contrast WARN is discharged, not dismissed.** Three light-mode fills sit
below 3:1 against the basemap. The skill's relief rule requires visible labels
or a table view — hence the labelled legend, which is load-bearing rather than
decorative, plus a hover card naming the exact `LU_DESC`. Do not delete either
without re-reading this.

**Known limit:** the dark set's worst *tritan* pair is ΔE 3.3
(`--lu-infra`↔`--lu-commerce`). The validator gates on min(protan, deutan) and
reports tritan separately; tritanopia is very rare, and the legend and hover
carry identity independently of colour. Recorded so it isn't rediscovered as a
surprise.

**Dark mode's `--lu-infra` was re-stepped after looking at it.** The first
passing dark olive (`#6f6600`) turned the Bishan MRT depot and the PIE corridor
into a mustard mass that outshouted the parcels that matter — infrastructure is
the *most* recessive class semantically (the basemap already draws roads) and
was the loudest visually. It sits at the chroma floor now (`#7c741c`, same
hue), re-validated as a set. The validator scores separation, not dominance;
that part still needs eyes.

---

## 7. Frontend behaviour worth not breaking

- **Filters.** Type, model and lease are *property*-level; period, size and
  price are *transaction*-level (a property vanishes only when none of its
  transactions qualify). Empty means unbounded — `0` is a real bound, so
  `Number(v) || null` would be wrong.
- **The map opens on 1Y (`DEFAULT_PERIOD`), and Reset returns there**, not to
  the full range — "reset" means "as I found it". `defaultStartIdx()` falls
  back to the full range when the history is shorter than the preset, so a
  young dataset never opens on nothing. Consequence to keep in mind: the
  filter badge reads `1` on load, because the period genuinely is narrowing
  the view. That is why `syncFilterBadge()` is also called from `boot` — with
  the old full-range default the badge was written only on the first
  interaction, which was invisible then and would now leave an empty badge
  sitting over a filtered map until something was touched.
- **Model chips group several raw models** (`MODEL_GROUPS` in `app.js`):
  Improved / Model A / Standard → **HDB**, Maisonette / Model A-Maisonette →
  **Maisonette**. Eleven chips became seven. The grouping lives entirely in
  `modelOf()`, so the chips, the facet counts and the filter all read it and
  cannot disagree — *and detail views deliberately do not*. The panel, compare
  columns and search list show `prop.model` raw, the same split the land-use
  layer uses between its six buckets and the exact `LU_DESC` on hover: group
  to make the control usable, never to hide the fact. Note the **HDB** model
  chip and the **HDB** source chip mean different things — DBSS, maisonettes
  and adjoined flats are HDB too and keep their own chips.
- **There is no Play button.** It swept a fixed window forward through time.
  Removed on request for header space, along with all of its machinery — if it
  ever comes back, note that `applyPreset` and `resetView` used to call
  `stopPlay()` first so an explicit choice wouldn't fight the timer.
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
- **The land-use layer is canvas-rendered and lazily fetched.** 6,361 polygons
  as SVG DOM nodes stalls the map on a phone, so it uses `L.canvas()`; and at
  623 KB gzipped it is fetched on first toggle, not at boot, with a cheap
  `HEAD` at boot only to decide whether the button is worth showing. It is
  rebuilt (not restyled) on a dark-mode change, because the colours are baked
  into the canvas at draw time.
- **Layer order is landparcels → P1 rings → school pins → property markers.**
  Within the overlay pane Leaflet stacks by insertion order, so the layer
  groups are created in that order in `initMap`. The properties are the
  subject; everything else is context.
- **The hover card has one positioning implementation** (`placeHoverCard`),
  shared by property markers and land parcels. It takes a container point
  rather than a marker precisely so the second caller couldn't fork it — the
  flip-and-clamp behaviour at the stage edges is the part nobody tests by hand.
- **Buttons are `white-space: nowrap`, and the action row scrolls on mobile.**
  "Land use" wrapped to two lines on a phone and made one button taller than
  the row; six actions then overflowed the viewport with no way to reach Reset.
  Same answer the model chips already use: one scrolling row, not a wrap.

---

## 8. Testing

`.venv/bin/python -m pytest tests/ -q` — **118 tests, ~0.4s, all offline.**
No test touches the network; fixtures live in `tests/fixtures/`.

Fixture values are **synthetic** but field names and shapes mirror the real
APIs exactly, and each file deliberately contains records the filters should
*reject*. See `tests/fixtures/README.md`.

Regression tests exist for every §5 item that bit once. Keep it that way.

---

## 9. Open items

**Answered but not built:**

- **PropertyGuru asking prices — not for this dashboard.** Behind a Cloudflare
  managed challenge (even `/robots.txt` returns a JS challenge; listing pages
  403). Getting past it means defeating bot detection, which is off the table.
  Separately, the repo and site are **public**, so redistributing their listing
  data is a bigger problem than personal use. A `propertyguru-daily-tracker`
  skill exists for personal, browser-driven, local tracking — keep it out of
  `ingest/`. Also note asking ≠ transacted: listings are aspirational, stale
  and duplicated across agents, and would corrupt every median and CAGR if
  blended in. Legitimate route at scale is a commercial data licence.

**Deliberately left out of the land-use overlay** (see §11 for why each):
GPR as its own visual encoding, parcel-level geometry clipping at the area
boundary, and any link between a property and the parcel it stands on.

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

---

## 11. Master Plan overlay — scope held deliberately

Four things it does not do. Each was considered and each has a reason, so a
future session can change one on purpose rather than "fix" it by accident.

- **GPR is not encoded visually.** Plot ratio is the most decision-relevant
  number in the layer, and a sequential ramp for it would be easy. It is on
  hover only, because a second visual encoding on the same polygons would have
  to compete with the land-use hue for the same channel, and because GPR is
  non-numeric for 63% of parcels island-wide (`LND`/`EVA`/`SDP`/null, §5.11) —
  a ramp with a majority "not applicable" class is a bad ramp. If it is ever
  wanted, the honest form is a *separate* mode that replaces the bucket
  colouring rather than layering on top of it.
- **Parcels are not geometrically clipped at the area boundary.** They are
  selected whole, by representative point, so the overlay's edge is the jagged
  union of parcel outlines rather than the planning-area line. True clipping
  needs polygon-polygon intersection against a concave boundary — Sutherland
  –Hodgman only handles convex clip regions, so it would mean vendoring a
  real geometry library into a repo that currently has none. The jagged edge
  is honest and costs nothing.
- **No property is linked to the parcel it stands on.** Tempting — "this flat
  sits on RESIDENTIAL, GPR 2.8" in the detail panel — and the point-in-polygon
  code to do it already exists. Held back because a property's geocode is a
  *building centroid from OneMap*, not a surveyed position: it is good to
  roughly a building's width, which is the same order as a parcel. Attributing
  a specific zoning to a specific block on that basis would present a
  coin-flip as a fact. The ground-truth spot checks in §5.11 all landed
  correctly, but spot checks on landmark condos are the easy case.
- **The overlay covers the areas the watchlist names, and no more.** Both
  sources contribute: a private entry's `planning_area` and an HDB `town`,
  which is the same administrative area under the same name (§5.9). Taking
  only the private entries was a real gap — adding Tampines 5-room flats put
  494 properties on the map with no parcels beneath them and nothing in the
  log to explain it. A watchlist of individually-named projects and no area at
  all still gets no overlay, and logs why; falling back to the whole island
  would ship 181 MB to draw context around five buildings. An HDB town that is
  not itself a planning area (`KALLANG/WHAMPOA`, `CENTRAL AREA` — each
  straddles several) is skipped and named in a warning rather than guessed
  at.

**If a new plan is gazetted:** run `--refresh-masterplan`, then check the log
for the "land uses not in any bucket" warning. A revision that adds a land-use
category still draws it (as `Other`) and names it in that warning; add it to
`BUCKETS` in `masterplan.py` and to the coverage test's list of 33.

---

## 12. Yield, phase and school comparison

### 12.1 Gross rental yield

    gross yield = rent psf x 12 / sale psf

Per-sqft on both sides, so floor area cancels and an HDB block quoted per unit
means the same thing as a condo quoted per sqft. Stored in its own `rentals`
table — a lease and a sale are different events, and a property can have one
without the other — keyed `(source, name, type, period)` and upserted, so a
re-run is idempotent like transactions.

Held deliberately:

- **Gross, never net.** No maintenance, tax, agent fee or vacancy. Labelled as
  such in the panel, because a 6.9% HDB figure is meaningfully different from
  what an owner actually banks.
- **The rent window is period-matched to the prices where it can be.** Where
  the selected price period predates published rents entirely, it falls back
  to the latest quarters *and says so* rather than silently pairing 2019
  prices with 2024 rents.
- **Only HDB quotes a contract count.** URA publishes a quarterly median and
  not the number of leases behind it, so the private card says "12 quarterly
  medians". Calling those 12 contracts would overstate what is known — this
  was in the first cut and was wrong.

### 12.2 Price phase — Momentum / Peaked / Cooling

A least-squares line fitted to **log(psf)** against time over the monthly
medians. Logs because a straight line in log space *is* a constant percentage
growth rate, which is what "rising" means for a price; the slope converts
directly to an annual rate.

The classification turns on **statistical significance, not an invented
threshold**. If the slope's t-statistic can't clear 2, the trend is not
distinguishable from flat and "Peaked" is reported — so a thin, noisy history
lands on Peaked instead of being talked into a direction. Under 6 months of
sales or 18 months of span there is no verdict at all.

Two things this must keep doing:

- **Show the fitted rate in every case, including Peaked.** A reader who
  disagrees can see the number the verdict came from.
- **Say it is not a forecast.** It describes prices already transacted. The
  wording was chosen so it cannot be read as prediction, and the caveat line
  is not decoration.

Sanity check on the current data — the distribution shifts from
momentum-dominant over 5Y (262 vs 36) to peaked-dominant over 2Y (40 vs 18),
which is the market actually cooling and is evidence the significance test is
doing work. Only one property classifies as Cooling; that is honest, not a bug
— Singapore residential prices have risen near-continuously since 2021.

### 12.3 Compare Schools

Clicking a school **toggles** it in or out of a comparison set, so one school
is just the one-element case rather than a separate mode.

- **Scope defaults to "Near all"** — the intersection. Finding properties in
  the overlap of several catchments is the stated purpose; "Near any" is
  offered as the union.
- **Ranked by the *farthest* school, not the nearest.** A property 200 m from
  one school and 1.9 km from another is a worse joint catchment than one
  1.1 km from both, and ranking on the nearest would put it first.
- **"Add another school" ranks candidates the same way** — by distance to the
  farthest existing pick, because a candidate only widens a usable
  intersection if it is near all of them.
- The index badge beside each distance is a **boxed element on its own line**.
  Inline, `1` immediately before `368 m` reads as `1368 m`; that was in the
  first cut and is the kind of thing only looking at rendered output catches.
- `.p-table td` sets `white-space: nowrap` for the transaction table. Project
  names are not short numbers, so the catchment table overrides it — without
  that, long names run over the distance column instead of wrapping.

**`refreshDetailViews()` now refreshes three views, not two.** The catchment
table is built from `visibleProperties()` and their median psf, so it goes
stale on every filter and period change exactly like the panel and the compare
drawer. This list is the single place that knowledge lives; it has been the
source of the same bug twice already (§7), so **anything new that reads the
filters belongs in it.**

### 12.4 P1 balloting: the cut-off is not a flag

MOE states one sentence per phase naming the distance band the school filled
at. **That band is a cut-off, not a yes/no per band.** "Balloting conducted for
children residing within 1km" means the school ran out *inside* 1 km — so
applicants in 1–2 km and beyond got nothing at all. Rendered as three
independent yes/no flags it tells the reader close to the opposite of what
happened, and this is the row someone makes a purchase decision on.

`band_outcomes` therefore returns one of three states per band:

| | |
|---|---|
| `in` | admitted without balloting — the cut-off was further out |
| `ballot` | balloting decided it — this is the cut-off band |
| `none` | the school filled before reaching this band |

Held deliberately:

- **MOE's own sentence is printed under every table.** The verdict is derived,
  so the source it was derived from stays visible — same principle as showing
  the fitted rate under the price phase.
- **An unrecognised sentence gets no verdict.** One 2025 note explains a PR
  intake cap and names no distance; it shows verbatim with no bands rather
  than a guess, and logs a warning so a new wording is noticed.
- **Zero vacancies means "not conducted", never "everyone got in."** The phase
  did not run. The first cut derived `applicants <= vacancies` → all bands
  admitted, which turned `0 for 0` into an encouraging green row.
- **Only 2A/2B/2C/2C-Supp are shown.** Phases 0 and 1 are sibling and alumni
  phases with no distance rule, so bands against them would be noise.
- **The bands are columns, not a stacked list**, so a phase reads across in one
  line and the three bands can be compared down a column.
- **MOE's sentence is the row tooltip, not a bullet list.** Printing it under
  every table was clutter; the derived verdict still has to be checkable, so it
  rides along on `title`.

**The colours were measured, and the obvious choice failed.** These three sit
in adjacent columns specifically to be compared across, so a traffic light is
the intuitive pick — and it is unreadable. CIEDE2000 on the pair a reader
compares most, "Balloted" against "Filled up":

| palette | worst CVD pair |
|---|---|
| green / orange / red | **3.9** (deuteranopia, light) |
| green / orange / grey | 5.7 |
| blue / orange / grey | **20.5** — shipped |

Red was wrong on meaning too: "Filled up" is not an error, it is "the school
never reached your band", which is what a recessive grey says. Blue for the
admitted state gives up the green-means-good intuition, but the word carries
that and the psf ramp never appears in this table. All three are existing role
tokens, so no new hex entered the palette. Same lesson as §6.1 — the intuitive
palette was rejected by measurement, not by taste.
