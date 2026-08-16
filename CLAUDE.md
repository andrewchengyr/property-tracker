# Singapore Property Tracker

**Read [PLAN.md](PLAN.md) before changing anything.** It records what was built,
the API behaviour discovered only by checking real output, and the design rules
being deliberately held — none of which is recoverable from the source alone.
Several bugs here recurred because a rule was applied in one place and not
another; §5 and §7 of that file list them.

Live: <https://andrewchengyr.github.io/property-tracker/> · refreshed weekly by
GitHub Actions (Mon 04:00 SGT).

## Quick orientation

```bash
.venv/bin/python -m pytest tests/ -q          # 143 tests, offline, ~0.4s
.venv/bin/python -m ingest.run --from-fixtures # offline run, no credentials
.venv/bin/python -m ingest.run                 # live run
python3 -m http.server 8123 --directory web    # then open localhost:8123
```

Add or remove properties by editing `config/watchlist.yaml` — no code changes.

## Things that will bite you

- **`data/transactions.db` is committed and must never be deleted or
  gitignored.** URA serves a rolling 5-year window; this archive is the only
  copy of anything older. `ingest/store.py` has no `DELETE`, by design.
- **Bump `?v=N` on both asset links in `web/index.html`** whenever `app.js` or
  `style.css` changes, or browsers serve a stale script against fresh markup.
- **`git pull` before local work.** The weekly bot commits refreshed data even
  when nothing changed, because `data.json` carries a timestamp.
- **Sources degrade, they never crash the run.** Keep it that way — this has
  been violated twice and both times took down unrelated sources.
- **Anything that reads the filters must be added to `refreshDetailViews()`.**
  It refreshes the property panel, the compare drawer *and* the school
  catchment table. A view left out of it silently shows stale numbers after a
  filter change — this exact bug has shipped twice. §12.3.
- **Rental coverage is partial and always will be** — ~20% of properties have
  no published rent. Yield shows an explicit reason, never a blank or a zero.
  The HDB rental dataset spells flat types `5-ROOM`; resale says `5 ROOM`.
  §5.12.
- **The land-use overlay is not rebuilt weekly.** `web/masterplan.json` is
  committed and only regenerated with `--refresh-masterplan`; the source is
  181 MB and the plan is gazetted about every five years. §5.11 and §11.
- **Colour is validated against what is actually rendered.** For the map fills
  that means the colour composited over the basemap, not the solid hex — §6.1
  records why the obvious translucent design is not readable at any opacity.
- **Don't commit or push unless asked.** Don't add watchlist entries that
  weren't requested.

## Credentials

`.env` (gitignored, already present locally): `URA_ACCESS_KEY`, `ONEMAP_EMAIL`,
`ONEMAP_PASSWORD`. Same three exist as GitHub Actions secrets. data.gov.sg
needs none. Both URA and OneMap throttle aggressively — a 403/400 on token
minting usually means throttling, not a bad credential.
