"""Entrypoint: watchlist → pull → geocode → dedup → store → export.

    python -m ingest.run                      # live pull
    python -m ingest.run --from-fixtures      # offline, from tests/fixtures
    python -m ingest.run --skip-hdb           # private only (no OneMap needed)

Resilience is the rule throughout: one property failing to fetch, parse or
geocode logs a warning and the run carries on with everything else.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from . import hdb as hdb_mod
from . import ura as ura_mod
from .geocode import Geocoder, OneMapClient, svy21_to_wgs84
from .models import SOURCE_HDB, SOURCE_URA, Transaction
from .store import DB_PATH, EXPORT_JSON, Store

log = logging.getLogger("ingest")

WATCHLIST = Path("config/watchlist.yaml")
FIXTURES = Path("tests/fixtures")


# ---------------------------------------------------------------------------
# Watchlist
# ---------------------------------------------------------------------------

def load_watchlist(path: Path | str = WATCHLIST) -> dict[str, list[dict[str, str]]]:
    """Forgiving by design — this file is hand-edited. Whitespace is trimmed,
    HDB fields uppercased to match the dataset, and bad entries warn instead
    of raising so one typo can't cost you the whole run."""
    path = Path(path)
    if not path.exists():
        log.warning("no watchlist at %s — nothing to do", path)
        return {"private": [], "hdb": []}

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        log.warning("watchlist is not a mapping; ignoring")
        return {"private": [], "hdb": []}

    private: list[dict[str, str]] = []
    for entry in raw.get("private") or []:
        project = _entry_value(entry, "project")
        if not project:
            log.warning("private entry without a project name, skipping: %r", entry)
            continue
        # URA caveats don't carry a completion year, so TOP is opt-in per entry.
        top = _entry_value(entry, "top_year")
        private.append({"project": project, "top_year": _year_or_none(top)})

    hdb: list[dict[str, str]] = []
    for entry in raw.get("hdb") or []:
        if not isinstance(entry, dict):
            log.warning("hdb entry is not a mapping, skipping: %r", entry)
            continue
        town = _upper(entry.get("town"))
        flat_type = _upper(entry.get("flat_type"))
        if not town or not flat_type:
            log.warning("hdb entry needs both town and flat_type, skipping: %r", entry)
            continue
        hdb.append(
            {
                "town": town,
                "flat_type": flat_type,
                "block": _upper(entry.get("block")),
                "street_name": _upper(entry.get("street_name")),
            }
        )

    log.info("watchlist: %d private, %d HDB", len(private), len(hdb))
    return {"private": private, "hdb": hdb}


def _entry_value(entry: Any, key: str) -> str:
    if isinstance(entry, dict):
        return str(entry.get(key) or "").strip()
    if isinstance(entry, str):  # tolerate a bare string instead of a mapping
        return entry.strip()
    return ""


def _upper(value: Any) -> str:
    return str(value or "").strip().upper()


def _year_or_none(value: Any) -> int | None:
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return year if 1800 < year < 2100 else None


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def collect_private(
    entries: list[dict[str, str]], store: Store, from_fixtures: bool
) -> list[Transaction]:
    if not entries:
        return []
    wanted = [e["project"] for e in entries]

    try:
        if from_fixtures:
            payload = json.loads((FIXTURES / "ura_transactions.json").read_text())
            projects = payload.get("Result") or []
        else:
            client = ura_mod.URAClient(os.getenv("URA_ACCESS_KEY", ""))
            projects = client.fetch_all()
    except Exception as exc:  # noqa: BLE001 — HDB can still succeed
        log.error("URA pull failed, continuing without private data: %s", exc)
        return []

    txns = ura_mod.normalize(projects, wanted, svy21_to_wgs84)
    log.info("URA: %d transactions across %d watchlist projects", len(txns), len(wanted))

    geocode_missing(txns, store, SOURCE_URA, from_fixtures)
    return txns


def collect_hdb(
    entries: list[dict[str, str]], store: Store, from_fixtures: bool
) -> list[Transaction]:
    if not entries:
        return []

    if from_fixtures:
        records: list[dict[str, Any]] = []
        payload = json.loads((FIXTURES / "hdb_resale.json").read_text())
        all_records = (payload.get("result") or {}).get("records") or []
        for entry in entries:
            subset = [
                r
                for r in all_records
                if _upper(r.get("town")) == entry["town"]
                and _upper(r.get("flat_type")) == entry["flat_type"]
            ]
            records.extend(
                hdb_mod.filter_records(subset, entry["block"], entry["street_name"])
            )
    else:
        client = hdb_mod.HDBClient()
        records = []
        for entry in entries:
            try:
                fetched = client.fetch(entry["town"], entry["flat_type"])
            except Exception as exc:  # noqa: BLE001 — skip this entry only
                log.error("HDB pull failed for %s / %s: %s", entry["town"], entry["flat_type"], exc)
                continue
            narrowed = hdb_mod.filter_records(fetched, entry["block"], entry["street_name"])
            if not narrowed:
                log.warning("HDB entry matched no records: %r", entry)
            records.extend(narrowed)

    txns = hdb_mod.normalize(records)
    log.info("HDB: %d transactions", len(txns))

    geocode_missing(txns, store, SOURCE_HDB, from_fixtures)
    return txns


def _onemap_client(from_fixtures: bool, store: Store, txns: list[Transaction]):
    if from_fixtures:
        _seed_fixture_geocodes(store, txns)
        return None
    if os.getenv("ONEMAP_EMAIL") and os.getenv("ONEMAP_PASSWORD"):
        return OneMapClient()
    log.warning(
        "ONEMAP_EMAIL / ONEMAP_PASSWORD not set — rows will have no coordinates "
        "and will not appear on the map (charts still work)"
    )
    return None


def geocode_missing(
    txns: list[Transaction], store: Store, source: str, from_fixtures: bool
) -> None:
    """Resolve coordinates for anything that arrived without them.

    Originally HDB-only — URA was documented as shipping SVY21 x/y with every
    caveat — but the live transaction feed returns records with no coordinates
    at all, so private projects need the same treatment or they never reach the
    map. One lookup per distinct building, cached in SQLite forever after.
    """
    missing = [t for t in txns if t.lat is None or t.lng is None]
    if not missing:
        return

    client = _onemap_client(from_fixtures, store, txns)
    geocoder = Geocoder(store, client)

    by_name: dict[str, list[Transaction]] = defaultdict(list)
    for t in missing:
        by_name[t.property_name].append(t)

    for name, group in by_name.items():
        address = group[0].address
        if source == SOURCE_URA:
            # OneMap indexes condo names, so the project name is the best key;
            # the street is the fallback when the name isn't in their gazetteer.
            candidates = [name, f"{name} {address}".strip(), address]
        else:
            # The country hint measurably improves HDB block matches.
            candidates = [name, f"{name} SINGAPORE"]

        found = None
        for query in candidates:
            if not query:
                continue
            found = geocoder.lookup(query)
            if found:
                break
        if not found:
            log.warning("no coordinates for %s — it will not appear on the map", name)
            continue

        lat, lng = found
        for t in group:
            t.lat, t.lng = lat, lng
        store.backfill_coords(name, source, lat, lng)

    log.info(
        "geocoding (%s): %d cached, %d fetched, %d unresolved",
        source, geocoder.hits, geocoder.misses, geocoder.failures,
    )


def _seed_fixture_geocodes(store: Store, txns: list[Transaction]) -> None:
    """Offline runs read coordinates from a saved OneMap response so the
    fixture path produces a complete, map-ready dataset."""
    path = FIXTURES / "onemap_search.json"
    if not path.exists():
        return
    fixture = json.loads(path.read_text())
    for address, payload in fixture.items():
        from .geocode import first_latlng

        found = first_latlng(payload)
        if found:
            store.put_geo(address, *found)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def summarize(txns: list[Transaction], added: int, updated: int, store: Store) -> None:
    per_property: dict[str, int] = defaultdict(int)
    for t in txns:
        per_property[f"[{t.source}] {t.property_name}"] += 1

    print("\n" + "=" * 62)
    print("RUN SUMMARY")
    print("=" * 62)
    if per_property:
        for name, n in sorted(per_property.items()):
            print(f"  {name:<44} {n:>6} txns")
    else:
        print("  no transactions matched the watchlist")
    print("-" * 62)
    print(f"  {'new rows':<44} {added:>6}")
    print(f"  {'existing rows refreshed':<44} {updated:>6}")
    print(f"  {'total rows in db':<44} {store.count():>6}")
    print("=" * 62 + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh Singapore property transactions")
    parser.add_argument("--watchlist", default=str(WATCHLIST))
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--json-out", default=str(EXPORT_JSON))
    parser.add_argument(
        "--from-fixtures",
        action="store_true",
        help="read saved API responses from tests/fixtures instead of the network",
    )
    parser.add_argument("--skip-ura", action="store_true", help="skip private residential")
    parser.add_argument("--skip-hdb", action="store_true", help="skip HDB resale")
    parser.add_argument("--no-csv", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    load_dotenv()

    watchlist = load_watchlist(args.watchlist)
    txns: list[Transaction] = []

    with Store(args.db) as store:
        if not args.skip_ura:
            txns.extend(collect_private(watchlist["private"], store, args.from_fixtures))
        if not args.skip_hdb:
            txns.extend(collect_hdb(watchlist["hdb"], store, args.from_fixtures))

        added, updated = store.upsert_many(txns)
        top_years = {
            e["project"]: e["top_year"]
            for e in watchlist["private"]
            if e.get("top_year")
        }
        store.export_json(args.json_out, top_years=top_years)
        if not args.no_csv:
            store.export_csv()
        summarize(txns, added, updated, store)

    return 0


if __name__ == "__main__":
    sys.exit(main())
