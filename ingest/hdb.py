"""data.gov.sg client — HDB resale flat prices.

No key required. `filters` does exact, case-sensitive matching and the dataset
stores values UPPERCASE, so town/flat_type are uppercased before they go out;
block/street_name are applied client-side because narrowing those server-side
would need exact spelling we can't count on from a hand-edited watchlist.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Iterable

import requests

from .models import SOURCE_HDB, Transaction, parse_hdb_month

log = logging.getLogger(__name__)

SEARCH_URL = "https://data.gov.sg/api/action/datastore_search"
# "Resale flat prices (based on registration date), from Jan 2017 onwards"
RESOURCE_ID = "d_8b84c4ee58e3cfc0ece0d773c8ca6abc"

PAGE_SIZE = 10000
TIMEOUT = 60
MAX_PAGES = 100  # backstop against a pagination bug spinning forever

# data.gov.sg rate-limits: a watchlist with several entries will trip 429 on
# back-to-back pulls. Retried with backoff rather than losing the entry.
MAX_RETRIES = 5
BACKOFF_BASE = 3  # seconds: 3, 6, 12, 24, 48


class HDBError(RuntimeError):
    pass


class HDBClient:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    def _get_with_retry(self, params: dict[str, Any]) -> requests.Response:
        """Retry on 429 and 5xx with exponential backoff, honouring Retry-After."""
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                r = self.session.get(SEARCH_URL, params=params, timeout=TIMEOUT)
                if r.status_code == 429 or r.status_code >= 500:
                    wait = float(r.headers.get("Retry-After") or BACKOFF_BASE * 2**attempt)
                    log.warning(
                        "data.gov.sg returned %d, retrying in %.0fs (attempt %d/%d)",
                        r.status_code, wait, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                r.raise_for_status()
                return r
            except requests.RequestException as exc:
                last = exc
                if attempt == MAX_RETRIES - 1:
                    break
                wait = BACKOFF_BASE * 2**attempt
                log.warning("data.gov.sg request failed (%s), retrying in %ds", exc, wait)
                time.sleep(wait)
        raise HDBError(f"data.gov.sg unreachable after {MAX_RETRIES} attempts: {last}")

    def fetch(self, town: str, flat_type: str) -> list[dict[str, Any]]:
        """Every record for a town + flat_type, following pagination."""
        filters = json.dumps({"town": town, "flat_type": flat_type})
        records: list[dict[str, Any]] = []
        offset = 0

        for _ in range(MAX_PAGES):
            r = self._get_with_retry(
                {
                    "resource_id": RESOURCE_ID,
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "filters": filters,
                }
            )
            payload = r.json()
            if not payload.get("success", True):
                raise HDBError(f"datastore_search failed: {payload!r}")

            result = payload.get("result") or {}
            page = result.get("records") or []
            records.extend(page)
            total = result.get("total")

            if not page or (total is not None and len(records) >= total):
                break
            offset += len(page)
        else:
            log.warning("HDB pagination hit MAX_PAGES for %s / %s", town, flat_type)

        log.info("HDB %s / %s: %d records", town, flat_type, len(records))
        return records


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


# The dataset abbreviates street names ("LOR 1A TOA PAYOH", not "LORONG 1A TOA
# PAYOH"), which is not what anyone types into a watchlist from memory. Both
# sides get canonicalised to the dataset's own abbreviations before comparing,
# so either spelling matches.
STREET_ABBREV = {
    "LORONG": "LOR", "STREET": "ST", "AVENUE": "AVE", "ROAD": "RD",
    "DRIVE": "DR", "CRESCENT": "CRES", "CENTRAL": "CTRL", "JALAN": "JLN",
    "UPPER": "UPP", "NORTH": "NTH", "SOUTH": "STH", "CLOSE": "CL",
    "PLACE": "PL", "TERRACE": "TER", "HEIGHTS": "HTS", "GARDENS": "GDNS",
    "BUKIT": "BT", "KAMPONG": "KG", "TANJONG": "TG", "MARKET": "MKT",
    "COMMONWEALTH": "C'WEALTH", "SAINT": "ST", "BLOCK": "BLK",
}


def canonical_street(value: Any) -> str:
    return " ".join(STREET_ABBREV.get(w, w) for w in _norm(value).split())


def filter_records(
    records: Iterable[dict[str, Any]],
    block: str | None = None,
    street_name: str | None = None,
    flat_model: str | None = None,
    lease_from: int | None = None,
) -> list[dict[str, Any]]:
    """Client-side narrowing that `filters` can't express.

    `flat_model` is how the dataset encodes "executive maisonette" — the
    flat_type is EXECUTIVE and the model is Maisonette; there is no
    "EXECUTIVE MAISONETTE" flat_type. `lease_from` bounds
    lease_commence_date, i.e. "built from this year onward".
    """
    want_block = _norm(block)
    want_street = canonical_street(street_name)
    want_model = _norm(flat_model)

    out = []
    for rec in records:
        if want_block and _norm(rec.get("block")) != want_block:
            continue
        if want_street and canonical_street(rec.get("street_name")) != want_street:
            continue
        if want_model and _norm(rec.get("flat_model")) != want_model:
            continue
        if lease_from is not None:
            year = _lease_year(rec.get("lease_commence_date"))
            # An unparseable lease year can't be shown to meet the bound.
            if year is None or year < lease_from:
                continue
        out.append(rec)
    return out


def _lease_year(value: Any) -> int | None:
    try:
        return int(str(value).strip()[:4])
    except (TypeError, ValueError):
        return None


def property_name(record: dict[str, Any]) -> str:
    """HDB has no project name — the building is the identity."""
    return f"{_norm(record.get('block'))} {_norm(record.get('street_name'))}".strip()


def normalize(records: Iterable[dict[str, Any]]) -> list[Transaction]:
    out: list[Transaction] = []
    for rec in records:
        try:
            out.append(_normalize_one(rec))
        except Exception as exc:  # noqa: BLE001 — skip the record, keep the run
            log.warning("skipping malformed HDB record %r: %s", rec.get("_id"), exc)
    return out


def _normalize_one(rec: dict[str, Any]) -> Transaction:
    name = property_name(rec)
    town = _norm(rec.get("town"))
    return Transaction(
        source=SOURCE_HDB,
        property_name=name,
        property_type=_norm(rec.get("flat_type")),
        segment=town,
        address=name,
        district_town=town,
        txn_date=parse_hdb_month(rec["month"]),
        price=float(rec["resale_price"]),
        area_sqm=float(rec["floor_area_sqm"]),
        storey_range=str(rec.get("storey_range") or "").strip(),
        tenure=str(rec.get("remaining_lease") or "").strip(),
        raw=rec,
    )
