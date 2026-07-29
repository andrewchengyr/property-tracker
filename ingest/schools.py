"""MOE primary schools — reference data for P1 registration distance.

Primary 1 registration priority depends on how far the home is from the
school, so the map needs school positions to draw the 1 km and 2 km bands
against. Only PRIMARY schools are fetched: the distance rule is a P1 rule and
secondary/JC entries would just clutter the map.

The directory gives postal codes rather than coordinates, so each school is
geocoded through OneMap and cached in the same SQLite `geo` table the property
addresses use — 179 lookups on the first run, none on any run after.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

import requests

from . import datagov

log = logging.getLogger(__name__)
# "School Directory and Information"
RESOURCE_ID = "d_688b934f82c1059ed0a6993d2a829089"

PAGE_SIZE = 1000
MAX_PAGES = 20
PRIMARY = "PRIMARY"

# Courtesy pause between first-time geocodes; the cache makes this a one-off.
GEOCODE_PAUSE = 0.12


class SchoolsError(RuntimeError):
    pass


def fetch_directory(session: requests.Session | None = None) -> list[dict[str, Any]]:
    session = session or requests.Session()
    records: list[dict[str, Any]] = []
    offset = 0

    for _ in range(MAX_PAGES):
        r = datagov.get(
            {"resource_id": RESOURCE_ID, "limit": PAGE_SIZE, "offset": offset},
            session=session,
        )
        payload = r.json()
        if not payload.get("success", True):
            raise SchoolsError(f"school directory request failed: {payload!r}")

        result = payload.get("result") or {}
        page = result.get("records") or []
        records.extend(page)
        total = result.get("total")
        if not page or (total is not None and len(records) >= total):
            break
        offset += len(page)

    log.info("school directory: %d records", len(records))
    return records


def primary_schools(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Just the primary schools, normalised to what the map needs."""
    out = []
    for rec in records:
        if str(rec.get("mainlevel_code") or "").strip().upper() != PRIMARY:
            continue
        postal = str(rec.get("postal_code") or "").strip()
        name = str(rec.get("school_name") or "").strip()
        if not name or not postal.isdigit():
            log.warning("skipping school with no usable postal code: %r", name or rec)
            continue
        out.append({
            "name": name,
            "address": " ".join(str(rec.get("address") or "").split()),
            "postal": postal,
            "area": str(rec.get("dgp_code") or "").strip(),
            "zone": str(rec.get("zone_code") or "").strip(),
            "url": str(rec.get("url_address") or "").strip(),
        })
    log.info("primary schools: %d", len(out))
    return out


def geocode_schools(schools: list[dict[str, Any]], geocoder) -> list[dict[str, Any]]:
    """Postal code first — it resolves to the exact building. The school name
    is a fallback for the rare postal code OneMap doesn't know."""
    located = []
    for school in schools:
        found = geocoder.cache.get_geo(school["postal"])
        if not found:
            found = geocoder.lookup(school["postal"]) or geocoder.lookup(school["name"])
            time.sleep(GEOCODE_PAUSE)
        if not found:
            log.warning("no coordinates for school %s (%s)", school["name"], school["postal"])
            continue
        school = dict(school)
        school["lat"], school["lng"] = found
        located.append(school)

    log.info("schools geocoded: %d of %d", len(located), len(schools))
    return located
