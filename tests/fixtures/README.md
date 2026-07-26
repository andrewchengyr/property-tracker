# Fixtures

Saved API responses so the ingestion can be built and tested with no keys and
no network.

| File | Stands in for |
|---|---|
| `ura_transactions.json` | `PMI_Resi_Transaction` — one `Result` array of projects, each with nested `transaction` records |
| `hdb_resale.json` | `datastore_search` — `result.records` + `result.total` |
| `onemap_search.json` | OneMap `elastic/search`, keyed by the search string |

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

To regenerate or extend them, edit the values by hand — they are plain JSON.
To capture *real* responses instead, save the raw JSON body of each call under
these names; the tests read them positionally, not by content.
