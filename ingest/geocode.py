"""Coordinates: SVY21→WGS84 for URA, OneMap lookups for HDB.

URA ships SVY21 eastings/northings with every caveat, so that path is a pure
projection. HDB ships only block + street, so it needs OneMap — which is rate
limited and token-gated, hence the two layers of caching (a persistent SQLite
`geo` table keyed by address, and a token cached to disk between runs).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Protocol

import requests

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.onemap.gov.sg/api/auth/post/getToken"
SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"
TOKEN_CACHE = Path(".onemap_token.json")
TIMEOUT = 30

SVY21 = "EPSG:3414"
WGS84 = "EPSG:4326"

_transformer = None


def svy21_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """URA gives x=easting, y=northing. Returns (lat, lng)."""
    global _transformer
    if _transformer is None:
        from pyproj import Transformer  # imported lazily — URA-only runs still work

        _transformer = Transformer.from_crs(SVY21, WGS84, always_xy=True)
    lng, lat = _transformer.transform(x, y)
    return lat, lng


class GeoCache(Protocol):
    """The slice of the store that the geocoder needs."""

    def get_geo(self, address: str) -> tuple[float, float] | None: ...
    def put_geo(self, address: str, lat: float, lng: float) -> None: ...


class OneMapError(RuntimeError):
    pass


class OneMapClient:
    """Token-authenticated address search. Tokens last ~3 days, so the token
    is cached to disk and only re-minted when it is missing or near expiry."""

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        session: requests.Session | None = None,
        token_cache: Path = TOKEN_CACHE,
    ):
        self.email = email or os.getenv("ONEMAP_EMAIL")
        self.password = password or os.getenv("ONEMAP_PASSWORD")
        self.session = session or requests.Session()
        self.token_cache = token_cache
        self._token: str | None = None

    def _cached_token(self) -> str | None:
        try:
            data = json.loads(self.token_cache.read_text())
        except (OSError, ValueError):
            return None
        # Refresh an hour early rather than discovering expiry mid-run.
        if float(data.get("expiry", 0)) - 3600 > time.time():
            return data.get("token")
        return None

    def token(self) -> str:
        if self._token:
            return self._token
        cached = self._cached_token()
        if cached:
            self._token = cached
            return cached
        if not self.email or not self.password:
            raise OneMapError("ONEMAP_EMAIL / ONEMAP_PASSWORD are not set")

        r = self.session.post(
            TOKEN_URL,
            json={"email": self.email, "password": self.password},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
        token = payload.get("access_token")
        if not token:
            raise OneMapError(f"no access_token in response: {payload!r}")

        self._token = token
        try:
            self.token_cache.write_text(
                json.dumps({"token": token, "expiry": float(payload.get("expiry_timestamp", 0))})
            )
        except OSError as exc:
            log.debug("could not cache OneMap token: %s", exc)
        log.info("OneMap token minted")
        return token

    def search(self, address: str) -> tuple[float, float] | None:
        r = self.session.get(
            SEARCH_URL,
            params={
                "searchVal": address,
                "returnGeom": "Y",
                "getAddrDetails": "Y",
                "pageNum": 1,
            },
            headers={"Authorization": self.token()},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return best_latlng(r.json(), address)


def best_latlng(payload: dict[str, Any], query: str = "") -> tuple[float, float] | None:
    """Centroid of a named development, else the first hit.

    A condo occupies several blocks and OneMap returns one row per block —
    Trevista comes back as blocks 21, 23 and 25 Lorong 3 Toa Payoh. Taking the
    first would pin the marker to whichever block happens to sort first, so
    when several hits share the searched BUILDING name their midpoint is used
    instead. HDB lookups match a single block and are unaffected.
    """
    wanted = (query or "").strip().upper()
    points = []
    for hit in payload.get("results") or []:
        if (hit.get("BUILDING") or "").strip().upper() != wanted:
            continue
        try:
            points.append((float(hit["LATITUDE"]), float(hit["LONGITUDE"])))
        except (TypeError, ValueError, KeyError):
            continue

    if len(points) > 1:
        n = len(points)
        return sum(p[0] for p in points) / n, sum(p[1] for p in points) / n

    return first_latlng(payload)


def first_latlng(payload: dict[str, Any]) -> tuple[float, float] | None:
    """Pull LATITUDE/LONGITUDE off the first search hit, if there is one."""
    results = payload.get("results") or []
    for hit in results:
        lat, lng = hit.get("LATITUDE"), hit.get("LONGITUDE")
        if lat not in (None, "") and lng not in (None, ""):
            try:
                return float(lat), float(lng)
            except (TypeError, ValueError):
                continue
    return None


class Geocoder:
    """Cache-first address→lat/lng. A miss that also fails upstream returns
    None; callers keep the row and simply leave it off the map."""

    def __init__(self, cache: GeoCache, client: OneMapClient | None = None):
        self.cache = cache
        self.client = client
        self.hits = 0
        self.misses = 0
        self.failures = 0

    def lookup(self, address: str) -> tuple[float, float] | None:
        address = (address or "").strip()
        if not address:
            return None

        cached = self.cache.get_geo(address)
        if cached:
            self.hits += 1
            return cached

        if self.client is None:
            self.failures += 1
            log.warning("no OneMap client configured; cannot geocode %r", address)
            return None

        try:
            found = self.client.search(address)
        except Exception as exc:  # noqa: BLE001 — a geocode failure is not fatal
            self.failures += 1
            log.warning("OneMap lookup failed for %r: %s", address, exc)
            return None

        if not found:
            self.failures += 1
            log.warning("OneMap found no match for %r", address)
            return None

        self.misses += 1
        self.cache.put_geo(address, *found)
        return found
