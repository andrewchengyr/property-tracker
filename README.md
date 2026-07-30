# Singapore Property Transaction Tracker

**Live map: https://andrewchengyr.github.io/property-tracker/**
Refreshed automatically every Monday at 04:00 SGT.

Tracks **transacted** (actually sold) prices for a watchlist of Singapore
properties — both HDB resale flats and private condos/landed — accumulates the
history in a committed SQLite database, and renders it on an interactive map
that works on desktop and phone.

Repo-native and free: no server, no paid services. GitHub Actions refreshes the
data weekly; GitHub Pages serves the map.

```
config/watchlist.yaml → ingest (URA + data.gov.sg + OneMap) → SQLite → web/data.json → Leaflet map
```

## Adding a property

Edit [`config/watchlist.yaml`](config/watchlist.yaml) and re-run the ingestion.
No code changes.

```yaml
private:                       # matched against URA project names
  - project: "TREVISTA"        # case-insensitive "contains"
    top_year: 2011             # optional — URA has no completion date

hdb:                           # filtered on the data.gov.sg dataset
  - town: "TOA PAYOH"          # required
    flat_type: "5 ROOM"        # required
    lease_from: 2000           # optional — min lease_commence_date
    street_name: "LOR 2 TOA PAYOH"   # optional
    block: "141"                     # optional
    flat_model: "Improved"           # optional
```

**`flat_type` is not what you'd guess for executive flats.** The dataset has no
`EXECUTIVE MAISONETTE` or `EXECUTIVE APARTMENT` flat type — both return zero
records. The flat type is `EXECUTIVE`, and maisonette vs apartment lives in
`flat_model`:

```yaml
  - town: "TOA PAYOH"
    flat_type: "EXECUTIVE"
    flat_model: "Maisonette"   # or "Apartment"
```

Valid `flat_type` values: `1 ROOM` … `5 ROOM`, `EXECUTIVE`, `MULTI-GENERATION`.
Common `flat_model` values: `Improved`, `Standard`, `Model A`, `New Generation`,
`Premium Apartment`, `Maisonette`, `Apartment`, `DBSS`, `Adjoined flat`.

`town` and `flat_type` are sent to the API; everything else is applied
client-side, so a too-narrow entry logs how many records it saw *before*
filtering — which tells you whether the town/type was wrong or just the
narrowing.

The loader trims whitespace and uppercases the HDB fields to match the dataset.
An entry that matches nothing logs a warning — it never aborts the run.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Offline, against saved API fixtures — no keys needed:

```bash
.venv/bin/python -m ingest.run --from-fixtures
```

Live:

```bash
cp .env.example .env   # then fill in the keys
.venv/bin/python -m ingest.run
```

Useful flags: `--skip-hdb` (private only, no OneMap needed), `--skip-ura`,
`--from-fixtures`, `--no-csv`, `-v`.

Re-running is always safe: upserts are keyed on the natural transaction
identity, so a second run updates rows rather than duplicating them.

## Viewing the map

```bash
python3 -m http.server 8123 --directory web
```

Then open <http://localhost:8123>. The map needs to be served over HTTP —
opening `index.html` from the filesystem will fail on the `data.json` fetch.

- Marker **colour** is median price per sqft (fixed scale, so moving the time
  slider changes a marker's value and never the meaning of a colour).
- Marker **shape** is the source — circle for private, square for HDB.
- The **period slider** rescopes every marker, chart and growth rate; **Play**
  sweeps it forward through time. Presets (**YTD, 1Y … 10Y, All**) sit under
  it, anchored to the newest month in the data rather than to today — the
  datasets lag reality by weeks, so "1Y from today" would clip the latest
  month. A preset longer than the history available is disabled rather than
  silently behaving as "All".
- **Filters** (size, price, lease) sit behind the disclosure in the top bar,
  with a badge showing how many are active. **Reset** clears all of them.
- **Hover a marker** for a summary card: median psf, growth rate, a sparkline,
  transaction count, median price and latest month.
- **Click a marker** for the full panel — growth, property facts, the
  price-per-sqft chart and recent transactions. The chart toggles between
  **Price psf** and **% growth**, rebased to the first month in the selected
  period. The setting is shared with the compare chart.
- **Compare** puts up to three properties side by side: one chart with a line
  each, plus a column of the same figures per property. Add them by clicking
  markers or by typing a name or street into the search box. Every number
  honours the current filters and period, so moving a slider re-reads the
  comparison live. A compared property that the filters exclude keeps its
  column and says so, rather than silently dropping out of your selection.
  Reorder by dragging a column, or with the ‹ › buttons on the chips and
  column headers. **The number follows position** — the leftmost card is
  always #1 — while the **colour follows the property**, so reordering or
  removing one never repaints the others. Number answers "where is it",
  colour answers "which is it", and the map badge shows both.
  The chart toggles between **Price psf** and **% growth**. Growth rebases each
  line to its own first month inside the selected period, so properties at very
  different price levels compare on one axis — a Toa Payoh flat and a Bishan
  condo can't share a price scale usefully, but they can share a growth one.
  Both modes follow the filters, so changing the period re-bases the chart.

### Filters

| Filter | Scope | Empty / zero means |
|---|---|---|
| Type | property | all sources |
| Period | transaction | full history |
| Size (sqft) | transaction | no lower / upper bound |
| Price (SGD) | transaction | no lower / upper bound |
| Lease remaining | property | no minimum |

**Size and price filter individual transactions**, so a property disappears
only once *none* of its transactions qualify — which is what makes "5-room
over 1,100 sqft under $1.4M" a question the map can answer. A transaction with
no recorded floor area can't satisfy a size bound and is excluded rather than
silently passed through.

A **histogram sits above the lease slider**, on the same scale, so each bar
stands over the position that selects it. Bars below the current minimum stay
drawn but grey — you can see what moving the slider back would return — and
clicking a bar sets the minimum to that band. Its counts ignore the lease
filter itself, so the distribution holds still while you drag rather than
reshaping as you approach it. Freehold has no years to run down and so can't
sit on the axis; it is counted next to the label (`+35 freehold`) rather than
silently missing.

**Lease remaining filters whole properties**, because tenure is a fact about
the building rather than any one sale. Freehold and unknown-lease properties
pass every minimum — a freehold outlasts any threshold, and hiding what can't
be assessed would quietly lose data. The slider is capped at the longest lease
actually present so the top of the track isn't dead travel.

The psf colour scale stays fixed while filtering, so a marker changing colour
always means its value moved, never that the scale shifted underneath it.

**The number on each model chip is a live count**: how many properties that
model would give you *with the filters you already have*. Every other filter is
applied to it, but the model selection itself is not — otherwise picking one
model would zero every other chip and leave nothing to navigate by. A model
with nothing behind it dims to zero but stays selectable, so the empty state
can explain itself.

### Schools and P1 distance

**Schools** in the top bar overlays all 179 MOE primary schools. Click one to
draw its **1 km** and **1–2 km** Primary 1 registration bands and list which
watched properties fall in each, nearest first, with distances.

Distance is **straight-line** — that is how MOE measures it, not walking or
driving distance. It runs from the school's registered postal code to each
block's geocoded position, so a block sitting within a few tens of metres of
the 1 km line should be checked against MOE's own tool rather than trusted
here.

The list respects the other filters, so "5-room under $1.2M within 1 km of this
school" is answerable in one pass.

Schools come from MOE's *School Directory and Information*
(`d_688b934f82c1059ed0a6993d2a829089`), filtered to `mainlevel_code = PRIMARY`
and geocoded by postal code into the same cached `geo` table the properties
use — 179 lookups once, none afterwards. `--skip-schools` skips the layer; if
`web/schools.json` is missing the map just hides the control.

### Growth rate

The panel and hover card show **compound annual growth in median psf** across
the selected period, measured between the first and last *monthly median*
rather than individual caveats, so a single high-floor sale at either end can't
set the headline. Under a year there is no annual rate to report, so the plain
change over the period is shown and labelled as such.

### Property facts

Tenure, lease start and **lease remaining as of today** are derived from the
source data — URA's `tenure` string (`99 yrs lease commencing from 2008`) and
HDB's `lease_commence_date`. The countdown is computed in the browser, so it
stays correct between ingestion runs.

`TOP` is shown for HDB automatically (an HDB lease commences on completion).
URA caveats don't carry a completion year, so for private projects set
`top_year` on the watchlist entry if you want it.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Every test runs against saved fixtures in `tests/fixtures/` — no keys, no
network.

## Secrets

Put these in `.env` locally (gitignored) and in **Settings → Secrets and
variables → Actions** on GitHub. Treat all three like passwords.

| Secret | Needed for | Get it from |
|---|---|---|
| `URA_ACCESS_KEY` | private residential | <https://eservice.ura.gov.sg/maps/api/reg.html> |
| `ONEMAP_EMAIL` / `ONEMAP_PASSWORD` | geocoding HDB addresses | <https://www.onemap.gov.sg/apidocs/register> |

data.gov.sg needs no key. If the watchlist has no `hdb` entries, OneMap is not
needed either — URA ships coordinates with every caveat.

## Deployment

`.github/workflows/refresh.yml` runs weekly (Mon 04:00 SGT) and on demand via
**Actions → Refresh transactions → Run workflow**. It pulls, commits the
refreshed `data/transactions.db` and `web/data.json`, and publishes `web/` to
Pages.

Enable it once under **Settings → Pages → Source: GitHub Actions**. (Pages can
only serve `/` or `/docs` from a branch, which is why the site is published as
a workflow artifact instead of by branch folder.)

## Data sources

| Source | Coverage | Auth |
|---|---|---|
| [URA Data Service](https://eservice.ura.gov.sg/maps/api/) | private residential caveats, rolling 5 years, 4 district batches | access key → daily token |
| [data.gov.sg](https://data.gov.sg/) `d_8b84c4ee58e3cfc0ece0d773c8ca6abc` | HDB resale, Jan 2017 onwards | none |
| [OneMap](https://www.onemap.gov.sg/) | geocoding HDB block + street | account → ~3-day token |

## Layout

```
config/watchlist.yaml   the control panel — edit this
ingest/                 run.py (entrypoint), ura.py, hdb.py, geocode.py, store.py, models.py
data/transactions.db    canonical store (committed)
data/exports/           CSV export
web/                    the site — index.html, app.js, style.css, data.json
tests/                  offline tests + saved API fixtures
```

## Notes and gotchas

- URA `contractDate` is **MMYY** — `0125` is January 2025, not the 25th.
- URA 403s without a browser `User-Agent`; the client always sends one.
- **URA no longer returns `x`/`y` coordinates** with transactions, despite the
  documented schema listing them. Every private row therefore arrives
  unmapped and is geocoded through OneMap like HDB — so OneMap credentials are
  needed for private property too, not just HDB. The SVY21→WGS84 conversion is
  still wired up for when coordinates are present.
- A project spans several blocks and OneMap returns one hit per block, so a
  multi-block match is averaged to the project centroid rather than pinned to
  whichever block sorts first.
- **Dedup keys must not contain derived fields.** Coordinates are filled in
  *after* a row is first stored, so keying on them made every second run
  re-insert instead of update. Identity is the transaction.
- URA data is a **rolling 5-year window** and past caveats can be revised or
  voided; URA itself advises keeping only the latest five years for accuracy.
  So private transactions start ~5 years before today and that boundary moves
  forward each week. There is no free per-caveat source for older private
  transactions — REALIS covers 1995 onwards but is a paid URA subscription,
  and data.gov.sg's URA datasets are quarterly *aggregates*, not individual
  transactions. The committed SQLite is the answer: it accumulates, so the
  archive outgrows the API window from the day it starts running.
- HDB is **not** subject to that limit. The dataset in use starts Jan 2017,
  but older resale datasets exist and are free — `1990-1999`
  (`d_ebc5ab87086db484f88045b47411ebc5`), `2000-Feb 2012`
  (`d_43f493c6c50d54243cc1eab0df142d6a`), `Mar 2012-Dec 2014`
  (`d_2d5ff9ea31397b66239f245f57751537`) and `Jan 2015-Dec 2016`
  (`d_ea9ed51da2787afaf8e51f827c304208`), ~746k records in total, same fields.
- data.gov.sg `filters` is exact-match and case-sensitive (values are
  UPPERCASE); `block`/`street_name` are therefore filtered client-side.
- **The dataset abbreviates street names** — `LOR 1A TOA PAYOH`, not `LORONG
  1A TOA PAYOH`. Writing the full word in the watchlist would otherwise match
  nothing at all, silently, so both spellings are canonicalised before
  comparing (`ingest/hdb.py`, `STREET_ABBREV`). Add any missing abbreviation
  there.
- OneMap tokens expire in ~3 days. Tokens are cached to `.onemap_token.json`
  and every geocode is cached in the SQLite `geo` table, so a known address is
  never looked up twice.
- A failed geocode leaves `lat`/`lng` null: the row is still stored and still
  charts, it just doesn't get a map marker. The header reports how many.
- HDB resale updates roughly monthly, URA roughly weekly — hence a weekly cron.
- data.gov.sg **rate-limits (429)** once a watchlist makes several calls in a
  row; the client retries with exponential backoff and honours `Retry-After`.
- HDB **towns are administrative, not geographic**. The `TOA PAYOH` town also
  contains Bidadari Park Drive (~2 km east, near Woodleigh), Potong Pasir Ave 1,
  Joo Seng Rd and Kim Keat Ave. A town-wide watchlist entry spreads the map
  considerably wider than the town name suggests.
- One block can hold **more than one flat type** (8 Joo Seng Rd has both 5 ROOM
  and EXECUTIVE) **and more than one model within a type** (236 Lor 1 Toa Payoh
  and 254 Kim Keat Ave each hold executive maisonettes *and* apartments, ~166
  vs ~142 sqm). The export therefore groups on name + type + model; grouping on
  name alone produced one marker with a blended psf under whichever label
  sorted last.
- `data/transactions.db` is committed, so a schema change has to cope with an
  older database being checked out by CI. `CREATE TABLE IF NOT EXISTS` will not
  add a column to a table that already exists — `Store._migrate()` does the
  additive `ALTER TABLE`s.
