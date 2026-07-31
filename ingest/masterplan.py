"""URA Master Plan 2025 land use — what is around a property, and what may be built.

A transacted price says what a place is worth today; the Master Plan is the only
free source for what the plot next door is *allowed to become*. It carries two
fields worth having: `LU_DESC` (the zoned land use) and `GPR` (gross plot ratio,
i.e. how much floor area may be built per unit of land).

Three things shape this module, all of them discovered by checking real output:

1. **The full layer is 181 MB / 113,394 parcels island-wide.** Nothing here can
   afford to ship that. Clipped to the planning areas the watchlist actually
   uses it is ~6.4k parcels, and with coordinates rounded and properties
   trimmed it exports at ~3.4 MB (~0.6 MB gzipped over the wire).

2. **It is a *plan*, not a feed.** MP2025 was gazetted 1 Dec 2025 and the
   previous edition ran from 2019 — a roughly five-yearly cycle. Re-downloading
   181 MB weekly to rebuild a file that changes twice a decade would also churn
   a committed 3.4 MB export on every run, so the refresh is opt-in
   (`--refresh-masterplan`) rather than part of the weekly cron.

3. **Land use is not a rainbow.** 82% of parcels in these areas are plain
   RESIDENTIAL, so residential is the *ground* the map is drawn on rather than
   one of the categories competing for a colour, and the remaining 22 land-use
   descriptions fold into six buckets. See `BUCKETS` for why those six.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

# "Master Plan 2025 Land Use layer" on data.gov.sg. Free, no credentials.
DATASET_ID = "d_a8c3546b26712e35021f3a681d0353ae"
POLL_URL = "https://api-open.data.gov.sg/v1/public/api/datasets/{}/poll-download"

TIMEOUT = 120
DOWNLOAD_TIMEOUT = 900          # 181 MB over a domestic line

# ~0.11 m at the equator. Parcel boundaries are surveyed to better than this,
# but nothing on a Leaflet map at zoom 19 can show the difference, and the
# extra digits the source ships (16 decimal places) cost more than the file.
PRECISION = 6


class MasterPlanError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Land-use buckets
# ---------------------------------------------------------------------------

# RESIDENTIAL is deliberately *not* a bucket. It is 82% of parcels in Toa Payoh
# and Bishan, so giving it one of six categorical colours would spend the
# scarcest resource on the least informative class — and the map's subject is
# already residential property. It is drawn as a quiet ground instead, and the
# six buckets below are the exceptions that answer "what else is around here".
GROUND = "homes"
GROUND_LABEL = "Homes"
GROUND_USES = ("RESIDENTIAL",)

# Ordered: the export, the legend and the palette all read this order, so a
# bucket's colour is fixed by its position and never by how many parcels it has.
BUCKETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("mixed", "Homes + shops", (
        "RESIDENTIAL WITH COMMERCIAL AT 1ST STOREY",
        "COMMERCIAL & RESIDENTIAL",
        "RESIDENTIAL / INSTITUTION",
    )),
    ("commerce", "Shops, offices & hotels", (
        "COMMERCIAL",
        "HOTEL",
        "COMMERCIAL / INSTITUTION",
        "WHITE",
    )),
    ("business", "Business & industry", (
        "BUSINESS 1",
        "BUSINESS 2",
        "BUSINESS PARK",
        "BUSINESS 1 - WHITE",
        "BUSINESS 2 - WHITE",
        "BUSINESS PARK - WHITE",
    )),
    ("civic", "Civic, health & education", (
        "CIVIC & COMMUNITY INSTITUTION",
        "EDUCATIONAL INSTITUTION",
        "PLACE OF WORSHIP",
        "HEALTH & MEDICAL CARE",
        "SPECIAL USE",
        "CEMETERY",
    )),
    ("green", "Park, water & open space", (
        "PARK",
        "OPEN SPACE",
        "SPORTS & RECREATION",
        "WATERBODY",
        "BEACH AREA",
        "AGRICULTURE",
    )),
    ("infra", "Transport, utility & reserve", (
        "ROAD",
        "TRANSPORT FACILITIES",
        "MASS RAPID TRANSIT",
        "LIGHT RAPID TRANSIT",
        "UTILITY",
        "PORT / AIRPORT",
        "RESERVE SITE",
    )),
)

# Anything the plan gains in a future revision. It gets a bucket rather than
# being dropped, and `build` logs the unrecognised descriptions by name — a new
# land-use category should be visible, not silently absent from the map.
OTHER = "other"
OTHER_LABEL = "Other"

_LOOKUP: dict[str, str] = {use: GROUND for use in GROUND_USES}
for _key, _label, _uses in BUCKETS:
    for _use in _uses:
        _LOOKUP[_use] = _key

# The plan's own shorthand where GPR is not a number. Expanded here rather than
# in the frontend so the meaning travels with the data.
GPR_CODES = {
    "LND": "Landed housing",
    "EVA": "Subject to evaluation",
    "SDP": "Subject to detailed planning",
}


def bucket_of(lu_desc: str | None) -> str:
    """Bucket key for a land-use description. Unknown values fall to `OTHER`
    rather than being dropped."""
    return _LOOKUP.get(str(lu_desc or "").strip().upper(), OTHER)


def legend() -> list[dict[str, str]]:
    """Bucket keys and labels, ground first, in drawing order."""
    out = [{"key": GROUND, "label": GROUND_LABEL}]
    out += [{"key": key, "label": label} for key, label, _ in BUCKETS]
    out.append({"key": OTHER, "label": OTHER_LABEL})
    return out


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def download_url(session: requests.Session | None = None) -> str:
    """Ask data.gov.sg for a signed download link. The link is short-lived, so
    it is fetched immediately before the download rather than cached."""
    session = session or requests.Session()
    r = session.get(POLL_URL.format(DATASET_ID), timeout=TIMEOUT)
    r.raise_for_status()
    payload = r.json()
    url = ((payload or {}).get("data") or {}).get("url")
    if not url:
        raise MasterPlanError(
            f"no download URL in poll response: {str(payload)[:200]}")
    return url


def fetch(session: requests.Session | None = None) -> list[dict[str, Any]]:
    """The whole island's parcels. 181 MB of JSON — the caller clips it."""
    session = session or requests.Session()
    url = download_url(session)
    log.info("master plan: downloading (181 MB, this takes a while)")
    r = session.get(url, timeout=DOWNLOAD_TIMEOUT)
    r.raise_for_status()
    features = (r.json() or {}).get("features") or []
    if not features:
        raise MasterPlanError("master plan download contained no features")
    log.info("master plan: %d parcels island-wide", len(features))
    return features


# ---------------------------------------------------------------------------
# Clip and shrink
# ---------------------------------------------------------------------------

def _rings(geometry: dict[str, Any]) -> Iterable[list]:
    """Outer rings only — a hole can't put a parcel inside an area on its own."""
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        if coords:
            yield coords[0]
    elif kind == "MultiPolygon":
        for poly in coords:
            if poly:
                yield poly[0]


def _shoelace(ring: list) -> float:
    """Signed area in square degrees. Only used to compare rings and to weight
    a centroid, so the lack of a projection doesn't matter."""
    total = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[i + 1][0], ring[i + 1][1]
        total += x0 * y1 - x1 * y0
    return total / 2.0


def _ring_centroid(ring: list) -> tuple[float, float] | None:
    area = _shoelace(ring)
    if not area:
        return None
    cx = cy = 0.0
    for i in range(len(ring) - 1):
        x0, y0 = ring[i][0], ring[i][1]
        x1, y1 = ring[i + 1][0], ring[i + 1][1]
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return cx / (6 * area), cy / (6 * area)


def representative_point(geometry: dict[str, Any]) -> tuple[float, float] | None:
    """One (lng, lat) that stands for the whole parcel — its largest ring's
    centroid, pulled back onto the ring if the shape is concave enough to put
    the centroid outside itself.

    A parcel is assigned to an area by this single point, not by "does any
    vertex fall inside". That distinction was found by looking at the rendered
    map rather than at the code: the Central Catchment is **one** OPEN SPACE
    parcel 8.1 km across, and because it touches Bishan, a vertex test dragged
    the whole of it onto the map — a green mass stretching from Bukit Panjang
    to Thomson on an overlay that claims to cover two planning areas.

    The cost is the opposite error: a parcel whose centre is just outside but
    which pokes in is dropped, leaving at most a lot's width of gap along the
    boundary. At a median parcel of ~230 m² that is invisible, and unlike the
    other error it cannot put a different part of the island on the map.
    """
    rings = [r for r in _rings(geometry) if r and len(r) >= 4]
    if not rings:
        return None
    ring = max(rings, key=lambda r: abs(_shoelace(r)))

    point = _ring_centroid(ring)
    if point and _point_in_ring(point[0], point[1], ring):
        return point
    # Degenerate or concave enough that the centroid escaped the shape: any
    # vertex is a fair stand-in and is guaranteed to be on the parcel.
    return ring[0][0], ring[0][1]


def _point_in_ring(lng: float, lat: float, ring: list) -> bool:
    """Ray casting, same convention as `planning._in_ring`."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            if lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
                inside = not inside
        j = i
    return inside


def _round(coords: Any, precision: int) -> Any:
    if coords and isinstance(coords[0], (int, float)):
        return [round(coords[0], precision), round(coords[1], precision)]
    return [_round(c, precision) for c in coords]


def _feature(raw: dict[str, Any], precision: int) -> dict[str, Any]:
    """Trimmed to what the map draws: bucket, exact use, plot ratio, geometry.

    The source ships ten properties per parcel (object ids, CRC hashes, update
    timestamps, shape areas). None of them survive — at 6,362 parcels they cost
    more than the geometry does.
    """
    props = raw.get("properties") or {}
    lu = str(props.get("LU_DESC") or "").strip()
    gpr = props.get("GPR")
    gpr = str(gpr).strip() if gpr is not None else ""
    return {
        "type": "Feature",
        "properties": {"b": bucket_of(lu), "lu": lu, "gpr": gpr},
        "geometry": {
            "type": raw["geometry"]["type"],
            "coordinates": _round(raw["geometry"]["coordinates"], precision),
        },
    }


def build(
    features: Iterable[dict[str, Any]],
    areas: dict[str, Any],
    precision: int = PRECISION,
) -> dict[str, Any]:
    """Clip island-wide parcels to the given planning areas and shrink them.

    `areas` is `{name: PlanningArea}` — the same committed polygons the
    watchlist's `planning_area` entries are decided by, so the overlay covers
    exactly the ground the properties are selected from and no more.
    """
    if not areas:
        raise MasterPlanError("no planning areas to clip to")

    kept: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    unknown: Counter[str] = Counter()
    scanned = 0

    for raw in features:
        scanned += 1
        geometry = raw.get("geometry") or {}
        if not geometry.get("coordinates"):
            continue
        point = representative_point(geometry)
        if point is None:
            continue
        lng, lat = point
        if not any(area.contains(lat, lng) for area in areas.values()):
            continue
        try:
            feature = _feature(raw, precision)
        except Exception as exc:  # noqa: BLE001 — one bad parcel isn't fatal
            log.warning("skipping unparseable parcel: %s", exc)
            continue
        bucket = feature["properties"]["b"]
        counts[bucket] += 1
        if bucket == OTHER:
            unknown[feature["properties"]["lu"]] += 1
        kept.append(feature)

    if unknown:
        # A future gazette can introduce a land-use category this module has
        # never seen. It still gets drawn, but say so by name — silently
        # bucketing it as "Other" is how a map goes quietly wrong.
        log.warning(
            "master plan: %d parcels have land uses not in any bucket: %s",
            sum(unknown.values()),
            ", ".join(f"{use} ({n})" for use, n in unknown.most_common()),
        )

    log.info(
        "master plan: %d of %d parcels inside %s (%s)",
        len(kept), scanned, ", ".join(sorted(areas)),
        ", ".join(f"{k}={v}" for k, v in counts.most_common()) or "none",
    )

    return {
        "areas": sorted(areas),
        "legend": legend(),
        "gpr_codes": GPR_CODES,
        "counts": dict(counts),
        "features": kept,
    }
