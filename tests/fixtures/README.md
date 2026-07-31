# Fixtures

Saved API responses so the ingestion can be built and tested with no keys and
no network.

| File | Stands in for |
|---|---|
| `ura_transactions.json` | `PMI_Resi_Transaction` — one `Result` array of projects, each with nested `transaction` records |
| `hdb_resale.json` | `datastore_search` — `result.records` + `result.total` |
| `onemap_search.json` | OneMap `elastic/search`, keyed by the search string |
| `masterplan.json` | URA Master Plan 2025 land use — a `FeatureCollection` of parcels with `LU_DESC` / `GPR` |

**The field names and shapes mirror the real APIs exactly; the values are
synthetic.** Prices follow a plausible Toa Payoh trajectory but they are
generated, not real transactions — they exist to exercise parsing, filtering,
dedup and the frontend, and are replaced the moment the ingestion runs against
live credentials (M3).

Each file deliberately contains records the watchlist should *reject*, so the
filters are actually tested rather than trivially satisfied:

- `ura_transactions.json` — three decoy projects alongside TREVISTA.
- `hdb_resale.json` — right town/wrong street (TOA PAYOH NORTH) and a different
  town entirely (BISHAN 4 ROOM).
- `masterplan.json` — a parcel in Jurong, a parcel with no geometry at all, a
  parcel straddling the Toa Payoh boundary (two vertices inside, centre
  outside — it must be *dropped*, see PLAN §5.11), and a land use no bucket
  knows about (which must still be drawn, and logged by name).

`masterplan.json` is the one fixture whose **coordinates are real**: the clip is
tested against the committed OneMap polygons, so the parcels have to sit in
actual Toa Payoh and Bishan for the test to mean anything. The attributes are
still synthetic.

To regenerate or extend them, edit the values by hand — they are plain JSON.
To capture *real* responses instead, save the raw JSON body of each call under
these names; the tests read them positionally, not by content.
