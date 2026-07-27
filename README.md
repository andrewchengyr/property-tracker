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

hdb:                           # filtered on the data.gov.sg dataset
  - town: "TOA PAYOH"          # required
    flat_type: "5 ROOM"        # required
    street_name: "LORONG 1A TOA PAYOH"   # optional
    block: "101"                         # optional
```

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
  sweeps it forward through time.
- **Filters** (size, price, lease) sit behind the disclosure in the top bar,
  with a badge showing how many are active. **Reset** clears all of them.
- **Hover a marker** for a summary card: median psf, growth rate, a sparkline,
  transaction count, median price and latest month.
- **Click a marker** for the full panel — growth, property facts, the
  price-per-sqft chart and recent transactions.

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

**Lease remaining filters whole properties**, because tenure is a fact about
the building rather than any one sale. Freehold and unknown-lease properties
pass every minimum — a freehold outlasts any threshold, and hiding what can't
be assessed would quietly lose data. The slider is capped at the longest lease
actually present so the top of the track isn't dead travel.

The psf colour scale stays fixed while filtering, so a marker changing colour
always means its value moved, never that the scale shifted underneath it.

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
- URA data is a rolling 5-year window and past caveats can be **revised or
  voided** — the weekly refresh keeps it current, so don't assume stored rows
  are immutable.
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
