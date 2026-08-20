"""Rental data, for gross rental yield.

Two sources, two shapes, one output. Everything is normalised to **rent per
sqft per month**, because that is what divides cleanly into the sale psf the
rest of the app already computes:

    gross yield = rent psf x 12 / sale psf

Area cancels out of that ratio, so it means the same thing for an HDB block
quoted per unit and a condo quoted per sqft.

Coverage is partial and always will be. URA only publishes a median where a
project had enough contracts in the quarter (39 of our 67 projects); HDB covers
most blocks but not the newest ones. A property with no rental data gets no
yield — never a zero, and never a silently omitted row.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable

import requests

from . import datagov
from .models import SOURCE_HDB, SOURCE_URA
from .ura import DATA_URL, USER_AGENT, URAClient

log = logging.getLogger(__name__)

# "Renting Out of Flats" — approved HDB subletting, block-level, monthly.
HDB_RESOURCE_ID = "d_c9f57187485a850908655db0e8cfe651"
HDB_PAGE = 10000
HDB_MAX_PAGES = 60

URA_SERVICE = "PMI_Resi_Rental_Median"
TIMEOUT = 90

SQFT_PER_SQM = 10.7639


def _norm(v: Any) -> str:
    return str(v or "").strip().upper()


# The resale and rental datasets disagree on this town's name, and on nothing
# else — every other town string is identical across the two. Verified by
# enumerating both; see the test.
TOWN_ALIASES = {"CENTRAL AREA": "CENTRAL"}


def canonical_town(value: Any) -> str:
    """Resale town name -> the name the rental dataset uses.

    Third field in a row where these two HDB datasets spell the same thing
    differently (streets in hdb.py, flat types below, now towns). The filter is
    applied server-side, so a wrong name returns an empty 200 and the town
    simply has no yield — assume nothing here without checking the values.
    """
    town = _norm(value)
    return TOWN_ALIASES.get(town, town)


def canonical_flat_type(value: Any) -> str:
    """The rental dataset writes `5-ROOM`; resale writes `5 ROOM`.

    Same trap as the street abbreviations in hdb.py — matching the two
    datasets on the raw string returns zero rows, silently.
    """
    return _norm(value).replace("-", " ").replace("  ", " ").strip()


# ---------------------------------------------------------------- HDB -------

def fetch_hdb(town: str, session: requests.Session | None = None) -> list[dict[str, Any]]:
    """Every approved rental for a town. Filtered server-side on town only;
    flat type is matched client-side because of the spelling difference."""
    records: list[dict[str, Any]] = []
    offset = 0
    for _ in range(HDB_MAX_PAGES):
        r = datagov.get(
            {
                "resource_id": HDB_RESOURCE_ID,
                "limit": HDB_PAGE,
                "offset": offset,
                "filters": json.dumps({"town": canonical_town(town)}),
            },
            session=session,
        )
        payload = r.json()
        result = payload.get("result") or {}
        page = result.get("records") or []
        records.extend(page)
        total = result.get("total")
        if not page or (total is not None and len(records) >= total):
            break
        offset += len(page)
    if not records:
        log.warning("HDB rentals %s: no records — check the town name against "
                    "the rental dataset's own spelling", town)
    log.info("HDB rentals %s: %d records", town, len(records))
    return records


def hdb_series(
    records: Iterable[dict[str, Any]], areas_sqft: dict[tuple[str, str], float]
) -> dict[tuple[str, str], list[tuple[str, float]]]:
    """(property_name, flat_type) -> [(YYYY-MM-01, rent psf per month), ...]

    HDB publishes rent per unit with no floor area, so the area comes from the
    property's own resale transactions. That makes the psf approximate for a
    block whose units vary in size — acceptable, because the alternative is no
    HDB yield at all, and the error is small within one flat type.
    """
    out: dict[tuple[str, str], list[tuple[str, float]]] = {}
    missing_area = set()

    for rec in records:
        name = f"{_norm(rec.get('block'))} {_norm(rec.get('street_name'))}".strip()
        ftype = canonical_flat_type(rec.get("flat_type"))
        key = (name, ftype)

        sqft = areas_sqft.get(key)
        if not sqft:
            missing_area.add(key)
            continue
        try:
            rent = float(rec.get("monthly_rent"))
            month = str(rec["rent_approval_date"]).strip()[:7] + "-01"
        except (TypeError, ValueError, KeyError):
            continue
        if rent <= 0:
            continue
        out.setdefault(key, []).append((month, rent / sqft))

    if missing_area:
        log.info(
            "HDB rentals: %d block/type combinations had no matching resale area "
            "and were skipped", len(missing_area),
        )
    return out


# ---------------------------------------------------------------- URA -------

def fetch_ura(client: URAClient | None = None) -> list[dict[str, Any]]:
    """Median rent per project per quarter, already in psf per month."""
    client = client or URAClient(os.getenv("URA_ACCESS_KEY", ""))
    r = client.session.get(
        DATA_URL,
        params={"service": URA_SERVICE},
        headers={
            "AccessKey": client.access_key,
            "Token": client.token(),
            "User-Agent": USER_AGENT,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("Status") != "Success":
        raise RuntimeError(f"{URA_SERVICE} rejected: {payload.get('Message', payload)!r}")
    result = payload.get("Result") or []
    log.info("URA rental medians: %d projects", len(result))
    return result


def quarter_to_month(ref: str) -> str | None:
    """`2024Q3` -> `2024-07-01`, the first month of that quarter."""
    s = str(ref or "").strip().upper()
    if len(s) != 6 or "Q" not in s:
        return None
    try:
        year, q = int(s[:4]), int(s[5])
    except ValueError:
        return None
    if not 1 <= q <= 4:
        return None
    return f"{year}-{(q - 1) * 3 + 1:02d}-01"


def ura_series(projects: Iterable[dict[str, Any]]) -> dict[str, list[tuple[str, float]]]:
    """project name (upper) -> [(YYYY-MM-01, rent psf per month), ...]"""
    out: dict[str, list[tuple[str, float]]] = {}
    for proj in projects:
        name = _norm(proj.get("project"))
        if not name:
            continue
        for entry in proj.get("rentalMedian") or []:
            month = quarter_to_month(entry.get("refPeriod"))
            median = entry.get("median")
            if not month or not median:
                continue
            try:
                value = float(median)
            except (TypeError, ValueError):
                continue
            if value > 0:
                out.setdefault(name, []).append((month, value))
    return out
