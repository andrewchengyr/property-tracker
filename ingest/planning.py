"""URA planning areas — the precise "is this in Toa Payoh?" test.

A URA caveat carries a postal district but no town, and districts are far
wider than the areas people mean: D12 is Balestier + Toa Payoh + Serangoon,
D20 is Ang Mo Kio + Bishan + Thomson. Selecting condos by district alone would
pull in Sembawang Hills and Thomson Rise for "Bishan".

So the boundary comes from OneMap's planning areas — the same official areas
HDB towns are named after — and a project is tested by point-in-polygon
against its geocoded position. The polygons are cached to disk and committed,
so a run needs no network for this and CI needs no extra call.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

AREAS_URL = "https://www.onemap.gov.sg/api/public/popapi/getAllPlanningarea"
CACHE = Path("data/planning_areas.json")
TIMEOUT = 90


class PlanningArea:
    """One area and its geometry, with a bounding box for a cheap early out."""

    __slots__ = ("name", "polygons", "bbox")

    def __init__(self, name: str, geometry: dict[str, Any]):
        self.name = name
        if geometry.get("type") == "Polygon":
            self.polygons = [geometry["coordinates"]]
        else:  # MultiPolygon
            self.polygons = list(geometry.get("coordinates") or [])

        xs = [c[0] for poly in self.polygons for ring in poly for c in ring]
        ys = [c[1] for poly in self.polygons for ring in poly for c in ring]
        self.bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else (0, 0, 0, 0)

    def contains(self, lat: float, lng: float) -> bool:
        min_x, min_y, max_x, max_y = self.bbox
        if not (min_x <= lng <= max_x and min_y <= lat <= max_y):
            return False
        return any(_in_polygon(lng, lat, poly) for poly in self.polygons)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PlanningArea {self.name}>"


def _in_polygon(lng: float, lat: float, polygon: list) -> bool:
    """First ring is the outer boundary; any others are holes."""
    if not polygon or not _in_ring(lng, lat, polygon[0]):
        return False
    return not any(_in_ring(lng, lat, hole) for hole in polygon[1:])


def _in_ring(lng: float, lat: float, ring: list) -> bool:
    """Ray casting. Coordinates are GeoJSON order: [lng, lat]."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lng < x_cross:
                inside = not inside
        j = i
    return inside


def load(token: str | None = None, cache: Path = CACHE,
         session: requests.Session | None = None) -> dict[str, PlanningArea]:
    """Areas by uppercase name. Fetches when a token is available and falls
    back to the committed cache, so a OneMap outage can't stop a run."""
    raw = None

    if token:
        try:
            session = session or requests.Session()
            r = session.get(AREAS_URL, headers={"Authorization": token}, timeout=TIMEOUT)
            r.raise_for_status()
            raw = r.json().get("SearchResults") or []
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(raw))
            log.info("planning areas: fetched %d and cached", len(raw))
        except Exception as exc:  # noqa: BLE001 — the cache is the fallback
            log.warning("planning area fetch failed (%s); using cache", exc)
            raw = None

    if raw is None:
        if not cache.exists():
            log.error("no planning area data and no cache at %s", cache)
            return {}
        raw = json.loads(cache.read_text())
        log.info("planning areas: %d from cache", len(raw))

    areas: dict[str, PlanningArea] = {}
    for entry in raw:
        name = str(entry.get("pln_area_n") or "").strip().upper()
        blob = entry.get("geojson")
        if not name or not blob:
            continue
        try:
            areas[name] = PlanningArea(name, json.loads(blob))
        except Exception as exc:  # noqa: BLE001 — one bad area isn't fatal
            log.warning("could not parse planning area %s: %s", name, exc)
    return areas
