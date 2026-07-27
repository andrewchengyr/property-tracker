"""URA Data Service client — private residential transactions (caveats).

Two-step auth: mint a daily token with the access key, then call the
transaction service with both. Data is a rolling 5-year window split across
4 batches by postal district, so a full pull means all 4.

HTTP and parsing are kept separate so the normalizer can be tested against
saved fixtures with no key and no network.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
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

# URA throttles token minting: several mints in quick succession start coming
# back 403 even with a valid key. Retried with backoff, and the token — valid
# for the day — is cached so repeated runs don't mint a fresh one each time.
MAX_RETRIES = 4
BACKOFF_BASE = 5  # seconds: 5, 10, 20
TOKEN_CACHE = Path(".ura_token.json")


class URAError(RuntimeError):
    pass


class URAClient:
    def __init__(
        self,
        access_key: str,
        session: requests.Session | None = None,
        token_cache: Path = TOKEN_CACHE,
    ):
        if not access_key:
            raise URAError("URA_ACCESS_KEY is not set")
        self.access_key = access_key
        self.session = session or requests.Session()
        self.token_cache = token_cache
        self._token: str | None = None

    def _headers(self, with_token: bool = False) -> dict[str, str]:
        h = {"AccessKey": self.access_key, "User-Agent": USER_AGENT}
        if with_token:
            if not self._token:
                raise URAError("token not minted yet")
            h["Token"] = self._token
        return h

    def _cached_token(self) -> str | None:
        """Tokens are valid for the day, so one minted earlier today will do."""
        try:
            data = json.loads(self.token_cache.read_text())
        except (OSError, ValueError):
            return None
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return data.get("token") if data.get("date") == today else None

    def token(self) -> str:
        if self._token:
            return self._token
        cached = self._cached_token()
        if cached:
            self._token = cached
            log.info("URA token reused from cache")
            return cached
        return self.mint_token()

    def mint_token(self) -> str:
        """Mint a fresh daily token, retrying through URA's throttling."""
        last = ""
        for attempt in range(MAX_RETRIES):
            r = self.session.get(TOKEN_URL, headers=self._headers(), timeout=TIMEOUT)
            if r.status_code in (403, 429) or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                if attempt < MAX_RETRIES - 1:
                    wait = BACKOFF_BASE * 2**attempt
                    log.warning(
                        "URA token mint returned %d (throttled?), retrying in %ds "
                        "(attempt %d/%d)", r.status_code, wait, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                break

            r.raise_for_status()
            payload = r.json()
            if payload.get("Status") != "Success" or not payload.get("Result"):
                raise URAError(f"token request rejected: {payload!r}")

            self._token = payload["Result"]
            try:
                self.token_cache.write_text(
                    json.dumps({
                        "token": self._token,
                        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    })
                )
            except OSError as exc:
                log.debug("could not cache URA token: %s", exc)
            log.info("URA token minted")
            return self._token

        raise URAError(
            f"token mint failed after {MAX_RETRIES} attempts ({last}). URA throttles "
            "minting; the key itself is probably fine if it worked recently."
        )

    def fetch_batch(self, batch: int, _retried: bool = False) -> list[dict[str, Any]]:
        r = self.session.get(
            DATA_URL,
            params={"service": SERVICE, "batch": batch},
            headers=self._headers(with_token=True),
            timeout=TIMEOUT,
        )
        # A cached token can be rejected if it expired since it was written.
        # Re-mint once rather than failing the whole pull on a stale cache.
        if r.status_code in (401, 403) and not _retried:
            log.warning("batch %d rejected with %d — re-minting token", batch, r.status_code)
            self._token = None
            self.mint_token()
            return self.fetch_batch(batch, _retried=True)

        r.raise_for_status()
        payload = r.json()
        if payload.get("Status") != "Success":
            raise URAError(f"batch {batch} rejected: {payload.get('Message', payload)!r}")
        return payload.get("Result") or []

    def fetch_all(self) -> list[dict[str, Any]]:
        """All 4 batches. A single failing batch logs and is skipped rather
        than losing the other three."""
        self.token()          # cached if one was minted earlier today
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
