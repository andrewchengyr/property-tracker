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
from . import masterplan as masterplan_mod
from . import planning
from . import schools as schools_mod
from . import ura as ura_mod
from .geocode import Geocoder, OneMapClient, svy21_to_wgs84
from .models import SOURCE_HDB, SOURCE_URA, Transaction
from .store import DB_PATH, EXPORT_JSON, Store

log = logging.getLogger("ingest")

WATCHLIST = Path("config/watchlist.yaml")

# "Condo" in the everyday sense: strata homes, not landed. URA's own landed
# types (Terrace, Detached, Semi-detached and their Strata variants) are
# excluded unless an entry names them explicitly.
DEFAULT_PRIVATE_TYPES = ("CONDOMINIUM", "APARTMENT", "EXECUTIVE CONDOMINIUM")
SCHOOLS_JSON = Path("web/schools.json")
MASTERPLAN_JSON = Path("web/masterplan.json")
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

    private: list[dict[str, Any]] = []
    for entry in raw.get("private") or []:
        project = _entry_value(entry, "project")
        area = _upper(entry.get("planning_area") if isinstance(entry, dict) else "")

        if not project and not area:
            log.warning(
                "private entry needs either a project name or a planning_area, "
                "skipping: %r", entry,
            )
            continue

        # URA caveats don't carry a completion year, so TOP is opt-in per entry.
        top = _entry_value(entry, "top_year")
        item: dict[str, Any] = {"project": project, "top_year": _year_or_none(top)}

        if area:
            item["planning_area"] = area
            # Districts bound how many projects have to be geocoded before the
            # polygon test can run; without one the whole island is a candidate.
            item["districts"] = [
                str(d).strip() for d in (entry.get("districts") or []) if str(d).strip()
            ]
            types = entry.get("property_types")
            item["property_types"] = (
                [_upper(t) for t in types] if types else list(DEFAULT_PRIVATE_TYPES)
            )
        private.append(item)

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
                # flat_model is how "executive maisonette" is really encoded:
                # flat_type EXECUTIVE + flat_model Maisonette.
                "flat_model": _upper(entry.get("flat_model")),
                "lease_from": _year_or_none(entry.get("lease_from")),
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
    entries: list[dict[str, str]],
    store: Store,
    from_fixtures: bool,
    errors: list[str] | None = None,
) -> list[Transaction]:
    if not entries:
        return []
    wanted = [e["project"] for e in entries if e.get("project")]

    try:
        if from_fixtures:
            payload = json.loads((FIXTURES / "ura_transactions.json").read_text())
            projects = payload.get("Result") or []
        else:
            client = ura_mod.URAClient(os.getenv("URA_ACCESS_KEY", ""))
            projects = client.fetch_all()
    except Exception as exc:  # noqa: BLE001 — HDB can still succeed
        log.error("URA pull failed, continuing without private data: %s", exc)
        if errors is not None:
            errors.append(f"URA pull failed: {exc}")
        return []

    area_types = _projects_in_areas(entries, projects, store, from_fixtures)
    # A project you named outright is never trimmed, even if an area entry
    # would also have picked it up — naming it is the stronger statement.
    for name in wanted:
        area_types.pop(name.strip().upper(), None)

    # Area-selected names are matched exactly, not as substrings — see
    # ura.normalize for why that distinction matters.
    txns = ura_mod.normalize(
        projects, wanted, svy21_to_wgs84, exact_names=area_types.keys())

    # An area entry selects a project if *any* of its units is a condo, but a
    # mixed development also holds strata terraces and semi-detached units —
    # which are not what "all condos in Toa Payoh" means. A project named
    # outright keeps everything; only area-selected ones are trimmed.
    before = len(txns)
    txns = [t for t in txns if _type_allowed(t, area_types)]
    if len(txns) < before:
        log.info("dropped %d landed transactions from area-selected projects",
                 before - len(txns))

    log.info("URA: %d transactions across %d projects (%d named, %d by area)",
             len(txns), len(wanted) + len(area_types), len(wanted), len(area_types))

    geocode_missing(txns, store, SOURCE_URA, from_fixtures)
    return txns


def _type_allowed(txn: Transaction, area_types: dict[str, set[str]]) -> bool:
    allowed = area_types.get(txn.property_name.strip().upper())
    return allowed is None or (txn.property_type or "").strip().upper() in allowed


def _projects_in_areas(
    entries: list[dict[str, Any]],
    projects: list[dict[str, Any]],
    store: Store,
    from_fixtures: bool,
) -> dict[str, set[str]]:
    """Project names inside a watchlist entry's planning area.

    URA gives a district but no town, so the district narrows the candidates
    and OneMap's planning-area polygon makes the actual call. Every candidate
    has to be geocoded before it can be tested; those geocodes are cached and
    reused by the coordinate backfill later, so nothing is looked up twice.
    """
    area_entries = [e for e in entries if e.get("planning_area")]
    if not area_entries:
        return {}

    # Every other source degrades rather than raising; this must too. Minting
    # a OneMap token here once threw straight out of the run and took HDB down
    # with it. Without a token we still have the committed polygons and the
    # cached geocodes, which is enough for an unchanged watchlist.
    client = None
    if not from_fixtures:
        client = _onemap_client(False, store, [])
        if client is not None:
            try:
                client.token()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "OneMap unavailable (%s) — falling back to cached geocodes "
                    "and cached boundaries; new projects can't be area-tested", exc,
                )
                client = None

    token = None
    if client is not None:
        try:
            token = client.token()
        except Exception:  # noqa: BLE001 — cache is the fallback
            token = None
    areas = planning.load(token=token)
    if not areas:
        log.error("no planning area boundaries available — skipping area entries")
        return {}

    geocoder = Geocoder(store, client)
    selected: dict[str, set[str]] = {}

    for entry in area_entries:
        area = areas.get(entry["planning_area"])
        if not area:
            log.warning(
                "unknown planning area %r (known: %s…)",
                entry["planning_area"], ", ".join(sorted(areas)[:5]),
            )
            continue

        districts = set(entry["districts"])
        types = set(entry["property_types"])
        candidates, undecidable, matched = [], 0, []

        for proj in projects:
            txns = proj.get("transaction") or []
            if not txns:
                continue
            if districts and (txns[0].get("district") or "").strip() not in districts:
                continue
            if types and not any(
                (t.get("propertyType") or "").strip().upper() in types for t in txns
            ):
                continue
            candidates.append(proj)

        for proj in candidates:
            name = (proj.get("project") or "").strip()
            street = (proj.get("street") or "").strip()
            found = None
            for query in (name, f"{name} {street}".strip(), street):
                if query:
                    found = geocoder.lookup(query)
                if found:
                    break
            if not found:
                undecidable += 1
                continue
            if area.contains(*found):
                matched.append(name)
                # A project reachable from two area entries keeps the union of
                # what either allows, rather than whichever ran last.
                selected.setdefault(name.upper(), set()).update(types)

        log.info(
            "%s: %d candidates in district(s) %s → %d inside the area"
            "%s",
            entry["planning_area"], len(candidates),
            ",".join(sorted(districts)) or "any", len(matched),
            f" ({undecidable} could not be geocoded and were skipped)" if undecidable else "",
        )

    return selected


def collect_hdb(
    entries: list[dict[str, str]],
    store: Store,
    from_fixtures: bool,
    errors: list[str] | None = None,
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
                hdb_mod.filter_records(
                    subset, entry["block"], entry["street_name"],
                    entry["flat_model"], entry["lease_from"],
                )
            )
    else:
        client = hdb_mod.HDBClient()
        records = []
        for entry in entries:
            try:
                fetched = client.fetch(entry["town"], entry["flat_type"])
            except Exception as exc:  # noqa: BLE001 — skip this entry only
                log.error("HDB pull failed for %s / %s: %s", entry["town"], entry["flat_type"], exc)
                if errors is not None:
                    errors.append(f"HDB pull failed for {entry['town']} / {entry['flat_type']}: {exc}")
                continue
            narrowed = hdb_mod.filter_records(
                fetched, entry["block"], entry["street_name"],
                entry["flat_model"], entry["lease_from"],
            )
            if not narrowed:
                log.warning(
                    "HDB entry matched no records: %s %s%s%s — %d fetched before "
                    "client-side filters",
                    entry["town"], entry["flat_type"],
                    f" model={entry['flat_model']}" if entry["flat_model"] else "",
                    f" lease_from={entry['lease_from']}" if entry["lease_from"] else "",
                    len(fetched),
                )
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


def collect_schools(
    store: Store, from_fixtures: bool, errors: list[str] | None = None
) -> list[dict[str, Any]]:
    """Primary schools, for the P1 distance rings. Reference data, not
    transactions, so it never touches the transactions table."""
    try:
        if from_fixtures:
            path = FIXTURES / "schools_directory.json"
            if not path.exists():
                return []
            records = (json.loads(path.read_text()).get("result") or {}).get("records") or []
        else:
            records = schools_mod.fetch_directory()
    except Exception as exc:  # noqa: BLE001 — the properties still matter
        log.error("school directory pull failed, keeping the last export: %s", exc)
        if errors is not None:
            errors.append(f"school directory pull failed: {exc}")
        return []

    wanted = schools_mod.primary_schools(records)
    if not wanted:
        return []

    client = None if from_fixtures else _onemap_client(False, store, [])
    geocoder = Geocoder(store, client)
    located = schools_mod.geocode_schools(wanted, geocoder)
    log.info(
        "school geocoding: %d cached, %d fetched, %d unresolved",
        geocoder.hits, geocoder.misses, geocoder.failures,
    )
    return located


def watchlist_areas(
    watchlist: dict[str, list[dict[str, Any]]],
    known: set[str] | None = None,
) -> list[str]:
    """Planning areas the watchlist covers, in stable order.

    **Both sources contribute.** A private entry names its `planning_area`
    outright; an HDB `town` is the same administrative area under the same name
    (§5.9). Deriving this from the private entries alone meant adding an HDB
    town in a new area left the land-use overlay silently behind — Tampines
    flats sitting over no parcels, with nothing in the log to say why.

    A town that is not itself a planning area is skipped and named in a
    warning rather than guessed at: HDB's `KALLANG/WHAMPOA` and `CENTRAL AREA`
    each straddle several, and picking one would quietly overlay the wrong
    ground.
    """
    seen: list[str] = []
    for entry in watchlist.get("private") or []:
        area = entry.get("planning_area")
        if area and area not in seen:
            seen.append(area)

    unmatched: list[str] = []
    for entry in watchlist.get("hdb") or []:
        town = entry.get("town")
        if not town or town in seen:
            continue
        if known is not None and town not in known:
            if town not in unmatched:
                unmatched.append(town)
            continue
        seen.append(town)

    if unmatched:
        log.warning(
            "HDB town(s) %s are not planning areas, so the land-use overlay "
            "does not cover them; name the area(s) explicitly if you want it to",
            ", ".join(unmatched),
        )
    return seen


def collect_masterplan(
    watchlist: dict[str, list[dict[str, Any]]],
    from_fixtures: bool,
    errors: list[str] | None = None,
) -> dict[str, Any] | None:
    """URA Master Plan 2025 land use, clipped to the watchlist's planning areas.

    Reference data like the schools layer: it never touches the transactions
    table, and a failure here must cost nothing else in the run.

    The clip uses the committed OneMap polygons (`planning.load()` with no
    token), so this needs no credentials at all — data.gov.sg serves the plan
    unauthenticated. Boundaries are resolved *before* the download, so a
    watchlist with nothing to clip to never spends 181 MB finding that out.
    """
    try:
        areas = planning.load()
        if not areas:
            log.error("no planning area boundaries available — skipping the overlay")
            return None

        wanted = watchlist_areas(watchlist, known=set(areas))
        if not wanted:
            log.info(
                "no planning areas in the watchlist — skipping the land-use overlay "
                "(it is clipped to those areas, so there is nothing to clip to)"
            )
            return None

        selected = {name: areas[name] for name in wanted}

        if from_fixtures:
            path = FIXTURES / "masterplan.json"
            if not path.exists():
                return None
            features = (json.loads(path.read_text()).get("features") or [])
        else:
            features = masterplan_mod.fetch()

        return masterplan_mod.build(features, selected)
    except Exception as exc:  # noqa: BLE001 — the properties still matter
        log.error("master plan pull failed, keeping the last export: %s", exc)
        if errors is not None:
            errors.append(f"master plan pull failed: {exc}")
        return None


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
    parser.add_argument("--skip-schools", action="store_true",
                        help="skip the MOE primary-school layer")
    parser.add_argument("--schools-out", default=str(SCHOOLS_JSON))
    # Off by default on purpose. The Master Plan is gazetted about every five
    # years, the source file is 181 MB, and the export is committed — so
    # rebuilding it weekly would spend the download and churn a 3.4 MB diff to
    # reproduce a file that hasn't changed. Pass this after a new gazette.
    parser.add_argument(
        "--refresh-masterplan",
        action="store_true",
        help="re-download the 181 MB URA Master Plan and rebuild the land-use "
             "overlay (only needed after a new plan is gazetted)",
    )
    parser.add_argument("--masterplan-out", default=str(MASTERPLAN_JSON))
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
    errors: list[str] = []

    with Store(args.db) as store:
        if not args.skip_ura:
            txns.extend(collect_private(watchlist["private"], store, args.from_fixtures, errors))
        if not args.skip_hdb:
            txns.extend(collect_hdb(watchlist["hdb"], store, args.from_fixtures, errors))

        if not args.skip_schools:
            store.export_schools(
                collect_schools(store, args.from_fixtures, errors), args.schools_out)

        # Rebuilt only when asked, or when the export doesn't exist yet — a
        # fresh clone with no overlay should get one on its first run without
        # having to know the flag.
        if args.refresh_masterplan or not Path(args.masterplan_out).exists():
            store.export_masterplan(
                collect_masterplan(watchlist, args.from_fixtures, errors),
                args.masterplan_out,
            )

        added, updated = store.upsert_many(txns)
        top_years = {
            e["project"]: e["top_year"]
            for e in watchlist["private"]
            if e.get("top_year") and e.get("project")
        }
        store.export_json(args.json_out, top_years=top_years)
        if not args.no_csv:
            store.export_csv()
        summarize(txns, added, updated, store)

    # Export and commit still happened above — the site keeps the last good
    # data. But a source that failed outright must not pass as a green run:
    # under a weekly cron, a revoked key or expired OneMap password would
    # otherwise go unnoticed until the numbers were badly stale.
    if errors:
        print("SOURCE FAILURES (exiting non-zero):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
