"""URA Data Service client — private residential transactions (caveats).

Two-step auth: mint a daily token with the access key, then call the
transaction service with both. Data is a rolling 5-year window split across
4 batches by postal district, so a full pull means all 4.

HTTP and parsing are kept separate so the normalizer can be tested against
saved fixtures with no key and no network.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

import requests

from .models import SOURCE_URA, Transaction, parse_ura_contract_date

log = logging.getLogger(__name__)

TOKEN_URL = "https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1"
DATA_URL = "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1"
SERVICE = "PMI_Resi_Transaction"
BATCHES = (1, 2, 3, 4)

# URA reliably 403s requests without a browser-shaped User-Agent.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

TIMEOUT = 60


class URAError(RuntimeError):
    pass


class URAClient:
    def __init__(self, access_key: str, session: requests.Session | None = None):
        if not access_key:
            raise URAError("URA_ACCESS_KEY is not set")
        self.access_key = access_key
        self.session = session or requests.Session()
        self._token: str | None = None

    def _headers(self, with_token: bool = False) -> dict[str, str]:
        h = {"AccessKey": self.access_key, "User-Agent": USER_AGENT}
        if with_token:
            if not self._token:
                raise URAError("token not minted yet")
            h["Token"] = self._token
        return h

    def mint_token(self) -> str:
        """Tokens are valid for the day; one per run is enough."""
        r = self.session.get(TOKEN_URL, headers=self._headers(), timeout=TIMEOUT)
        r.raise_for_status()
        payload = r.json()
        if payload.get("Status") != "Success" or not payload.get("Result"):
            raise URAError(f"token request rejected: {payload!r}")
        self._token = payload["Result"]
        log.info("URA token minted")
        return self._token

    def fetch_batch(self, batch: int) -> list[dict[str, Any]]:
        r = self.session.get(
            DATA_URL,
            params={"service": SERVICE, "batch": batch},
            headers=self._headers(with_token=True),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        if payload.get("Status") != "Success":
            raise URAError(f"batch {batch} rejected: {payload.get('Message', payload)!r}")
        return payload.get("Result") or []

    def fetch_all(self) -> list[dict[str, Any]]:
        """All 4 batches. A single failing batch logs and is skipped rather
        than losing the other three."""
        if not self._token:
            self.mint_token()
        projects: list[dict[str, Any]] = []
        for batch in BATCHES:
            try:
                got = self.fetch_batch(batch)
                log.info("URA batch %d: %d projects", batch, len(got))
                projects.extend(got)
            except Exception as exc:  # noqa: BLE001 — one batch must not kill the run
                log.warning("URA batch %d failed, skipping: %s", batch, exc)
        return projects


def matches_watchlist(project_name: str, wanted: Iterable[str]) -> bool:
    """Case-insensitive substring match, as the spec specifies."""
    name = (project_name or "").strip().lower()
    return any(w and w in name for w in wanted)


def normalize(
    projects: list[dict[str, Any]],
    wanted_projects: Iterable[str],
    to_wgs84: Callable[[float, float], tuple[float, float]],
) -> list[Transaction]:
    """Flatten URA's project→transaction nesting into normalized rows.

    `to_wgs84` takes SVY21 (x, y) and returns (lat, lng); injected so this
    stays testable without pulling in pyproj at import time.
    """
    wanted = [w.strip().lower() for w in wanted_projects if w and w.strip()]
    if not wanted:
        return []

    out: list[Transaction] = []
    matched_names: set[str] = set()

    for proj in projects:
        name = (proj.get("project") or "").strip()
        if not matches_watchlist(name, wanted):
            continue
        matched_names.add(name)

        street = (proj.get("street") or "").strip()
        segment = (proj.get("marketSegment") or "").strip()

        for txn in proj.get("transaction") or []:
            try:
                row = _normalize_one(txn, name, street, segment, to_wgs84)
            except Exception as exc:  # noqa: BLE001 — skip the record, keep the run
                log.warning("skipping malformed URA txn for %s: %s", name, exc)
                continue
            out.append(row)

    for w in wanted:
        if not any(w in m.lower() for m in matched_names):
            log.warning("watchlist project %r matched no URA project", w)

    return out


def _normalize_one(
    txn: dict[str, Any],
    project: str,
    street: str,
    segment: str,
    to_wgs84: Callable[[float, float], tuple[float, float]],
) -> Transaction:
    lat = lng = None
    x, y = txn.get("x"), txn.get("y")
    if x not in (None, "") and y not in (None, ""):
        try:
            lat, lng = to_wgs84(float(x), float(y))
        except Exception as exc:  # noqa: BLE001 — an unmappable row still charts
            log.warning("SVY21 conversion failed for %s (%s, %s): %s", project, x, y, exc)

    return Transaction(
        source=SOURCE_URA,
        property_name=project,
        property_type=(txn.get("propertyType") or "").strip(),
        segment=segment,
        address=street,
        district_town=(txn.get("district") or "").strip(),
        txn_date=parse_ura_contract_date(txn["contractDate"]),
        price=float(txn["price"]),
        area_sqm=float(txn["area"]),
        storey_range=(txn.get("floorRange") or "").strip(),
        tenure=(txn.get("tenure") or "").strip(),
        lat=lat,
        lng=lng,
        raw=txn,
    )
