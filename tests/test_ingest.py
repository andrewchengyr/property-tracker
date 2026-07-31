"""Offline tests — every one runs against saved fixtures, no keys, no network."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest
import requests

from ingest import datagov
from ingest import hdb as hdb_mod
from ingest import geocode as geocode_mod
from ingest import masterplan
from ingest import planning
from ingest import schools as schools_mod
from ingest import ura as ura_mod
from ingest.geocode import Geocoder, first_latlng, svy21_to_wgs84
from ingest.models import (
    SOURCE_HDB,
    SOURCE_URA,
    lease_facts,
    parse_hdb_month,
    parse_ura_contract_date,
)
from ingest.run import collect_masterplan, load_watchlist, watchlist_areas
from ingest.store import Store, slugify

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def ura_projects():
    return json.loads((FIXTURES / "ura_transactions.json").read_text())["Result"]


@pytest.fixture
def hdb_records():
    return json.loads((FIXTURES / "hdb_resale.json").read_text())["result"]["records"]


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


# --------------------------------------------------------------- dates -----

@pytest.mark.parametrize(
    "raw,expected",
    [("0324", date(2024, 3, 1)), ("0125", date(2025, 1, 1)), ("1221", date(2021, 12, 1))],
)
def test_ura_contract_date_is_mmyy_not_yymm(raw, expected):
    assert parse_ura_contract_date(raw) == expected


@pytest.mark.parametrize("bad", ["3024", "abcd", "324", "", "0013"])
def test_malformed_contract_date_raises(bad):
    with pytest.raises(ValueError):
        parse_ura_contract_date(bad)


def test_hdb_month_parses():
    assert parse_hdb_month("2025-03") == date(2025, 3, 1)


# --------------------------------------------------------------- lease -----

@pytest.mark.parametrize(
    "tenure,label,start,years",
    [
        ("99 yrs lease commencing from 2008", "99-year leasehold", 2008, 99),
        ("999 yrs lease commencing from 1875", "999-year leasehold", 1875, 999),
        ("Freehold", "Freehold", None, None),
        ("freehold", "Freehold", None, None),
        ("99 yrs from 2008", "99 yrs from 2008", None, None),   # unparseable: verbatim
        ("", "", None, None),
    ],
)
def test_ura_tenure_parsing(tenure, label, start, years):
    f = lease_facts(SOURCE_URA, tenure)
    assert f["tenure_label"] == label
    assert f["lease_start"] == start
    assert f["lease_years"] == years


def test_hdb_lease_comes_from_commencement_not_remaining():
    """`remaining_lease` is only true as of that caveat; the start year is the
    stable fact the frontend counts down from."""
    f = lease_facts(
        SOURCE_HDB,
        "84 years 11 months",
        {"lease_commence_date": "2012", "flat_model": "DBSS"},
    )
    assert f == {
        "tenure_label": "99-year leasehold",
        "lease_start": 2012,
        "lease_years": 99,
        "flat_model": "DBSS",
    }


def test_hdb_lease_survives_a_missing_commencement_date():
    f = lease_facts(SOURCE_HDB, "70 years", {})
    assert f["lease_start"] is None and f["lease_years"] == 99


def test_export_carries_lease_facts_and_top_year(store, hdb_records, tmp_path):
    store.upsert_many(
        hdb_mod.normalize(
            hdb_mod.filter_records(hdb_records, street_name="LOR 1A TOA PAYOH")))
    payload = store.export_json(tmp_path / "d.json")
    prop = payload["properties"][0]
    assert prop["tenure_label"] == "99-year leasehold"
    assert prop["lease_years"] == 99
    # HDB leases commence on completion, so TOP falls out of the lease start.
    assert prop["top_year"] == prop["lease_start"]


def test_watchlist_top_year_reaches_the_export(store, ura_projects, tmp_path):
    store.upsert_many(ura_mod.normalize(ura_projects, ["TREVISTA"], svy21_to_wgs84))
    payload = store.export_json(tmp_path / "d.json", top_years={"trevista": 2011})
    prop = [p for p in payload["properties"] if p["source"] == SOURCE_URA][0]
    assert prop["top_year"] == 2011
    assert prop["lease_start"] == 2007      # from the fixture's tenure string


# ------------------------------------------------------------ projection ----

def test_svy21_converts_into_singapore():
    # Trevista's approximate SVY21 easting/northing.
    lat, lng = svy21_to_wgs84(30599.0, 34405.0)
    assert 1.20 < lat < 1.48
    assert 103.6 < lng < 104.1


# ----------------------------------------------------------------- URA ------

def test_normalize_keeps_only_watchlist_projects(ura_projects):
    txns = ura_mod.normalize(ura_projects, ["TREVISTA"], svy21_to_wgs84)
    assert txns
    assert {t.property_name for t in txns} == {"TREVISTA"}


def test_project_match_is_case_insensitive_substring(ura_projects):
    lower = ura_mod.normalize(ura_projects, ["trevista"], svy21_to_wgs84)
    partial = ura_mod.normalize(ura_projects, ["trevis"], svy21_to_wgs84)
    assert len(lower) == len(partial) > 0


def test_exact_names_do_not_match_by_substring():
    """Planning-area selection yields exact API names. Matching those as
    substrings pulled THE ORIENT (Pasir Panjang) in behind THE ORIE (Lorong 1
    Toa Payoh) and put a project from across the island on the map."""
    txn = {"contractDate": "0324", "price": "1000000", "area": "100",
           "propertyType": "Condominium", "district": "12", "floorRange": "01-05",
           "tenure": "99 yrs"}
    projects = [
        {"project": "THE ORIE", "street": "LORONG 1 TOA PAYOH",
         "marketSegment": "RCR", "transaction": [txn]},
        {"project": "THE ORIENT", "street": "PASIR PANJANG ROAD",
         "marketSegment": "RCR", "transaction": [txn]},
    ]
    exact = ura_mod.normalize(projects, [], svy21_to_wgs84, exact_names=["THE ORIE"])
    assert {t.property_name for t in exact} == {"THE ORIE"}

    # The substring path is still substring-matching, for hand-typed entries.
    loose = ura_mod.normalize(projects, ["THE ORIE"], svy21_to_wgs84)
    assert {t.property_name for t in loose} == {"THE ORIE", "THE ORIENT"}


def test_exact_and_substring_names_combine():
    txn = {"contractDate": "0324", "price": "1000000", "area": "100",
           "propertyType": "Condominium", "district": "12", "floorRange": "01-05",
           "tenure": "99 yrs"}
    projects = [
        {"project": "TREVISTA", "street": "A", "marketSegment": "RCR", "transaction": [txn]},
        {"project": "SKY VUE", "street": "B", "marketSegment": "RCR", "transaction": [txn]},
        {"project": "OTHER", "street": "C", "marketSegment": "RCR", "transaction": [txn]},
    ]
    txns = ura_mod.normalize(projects, ["trevis"], svy21_to_wgs84, exact_names=["SKY VUE"])
    assert {t.property_name for t in txns} == {"TREVISTA", "SKY VUE"}


def test_empty_watchlist_yields_nothing(ura_projects):
    assert ura_mod.normalize(ura_projects, [], svy21_to_wgs84) == []


def test_ura_rows_are_fully_normalized(ura_projects):
    t = ura_mod.normalize(ura_projects, ["TREVISTA"], svy21_to_wgs84)[0]
    assert t.source == SOURCE_URA
    assert t.address == "LORONG 1 TOA PAYOH"
    assert t.segment == "RCR"
    assert t.price > 0 and t.area_sqm > 0
    assert t.area_sqft == pytest.approx(t.area_sqm * 10.7639)
    assert t.price_psf == pytest.approx(t.price / t.area_sqft)
    assert 1.20 < t.lat < 1.48 and 103.6 < t.lng < 104.1


def test_one_malformed_record_does_not_lose_the_rest():
    projects = [{
        "project": "TREVISTA", "street": "LORONG 1 TOA PAYOH", "marketSegment": "RCR",
        "transaction": [
            {"contractDate": "NOPE", "price": "1", "area": "100"},          # bad date
            {"contractDate": "0324", "price": "2000000", "area": "100",
             "propertyType": "Condominium", "district": "12",
             "floorRange": "06-10", "tenure": "99 yrs", "x": "30599", "y": "34405"},
        ],
    }]
    txns = ura_mod.normalize(projects, ["TREVISTA"], svy21_to_wgs84)
    assert len(txns) == 1
    assert txns[0].txn_date == date(2024, 3, 1)


def test_missing_coords_leave_row_intact_without_latlng():
    projects = [{
        "project": "TREVISTA", "street": "X", "marketSegment": "RCR",
        "transaction": [{"contractDate": "0324", "price": "2000000", "area": "100",
                         "propertyType": "Condominium", "district": "12",
                         "floorRange": "06-10", "tenure": "99 yrs"}],
    }]
    txns = ura_mod.normalize(projects, ["TREVISTA"], svy21_to_wgs84)
    assert len(txns) == 1 and txns[0].lat is None


# ----------------------------------------------------- URA token handling ---

class _FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {"Status": "Success", "Result": "tok-123"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Replays a queue of responses and records the calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        return self.responses.pop(0) if self.responses else _FakeResponse()


def test_ura_token_retries_through_throttling(tmp_path, monkeypatch):
    """URA answers 403 to rapid re-minting even with a valid key — that took
    down a scheduled run. It must back off and retry, not give up."""
    monkeypatch.setattr(ura_mod.time, "sleep", lambda s: None)
    session = _FakeSession([_FakeResponse(403), _FakeResponse(403), _FakeResponse(200)])
    client = ura_mod.URAClient("key", session=session, token_cache=tmp_path / "t.json")
    assert client.mint_token() == "tok-123"
    assert session.calls == 3


def test_ura_token_gives_up_with_a_useful_message(tmp_path, monkeypatch):
    monkeypatch.setattr(ura_mod.time, "sleep", lambda s: None)
    session = _FakeSession([_FakeResponse(403)] * ura_mod.MAX_RETRIES)
    client = ura_mod.URAClient("key", session=session, token_cache=tmp_path / "t.json")
    with pytest.raises(ura_mod.URAError, match="throttles"):
        client.mint_token()


def test_ura_token_is_cached_for_the_day(tmp_path):
    cache = tmp_path / "t.json"
    s1 = _FakeSession([_FakeResponse(200)])
    ura_mod.URAClient("key", session=s1, token_cache=cache).mint_token()
    assert s1.calls == 1

    s2 = _FakeSession([_FakeResponse(200)])       # fresh client, same cache
    client = ura_mod.URAClient("key", session=s2, token_cache=cache)
    assert client.token() == "tok-123"
    assert s2.calls == 0                          # reused, no second mint


def test_yesterdays_cached_token_is_not_reused(tmp_path):
    cache = tmp_path / "t.json"
    cache.write_text(json.dumps({"token": "stale", "date": "2000-01-01"}))
    session = _FakeSession([_FakeResponse(200)])
    client = ura_mod.URAClient("key", session=session, token_cache=cache)
    assert client.token() == "tok-123"            # re-minted, not the stale one
    assert session.calls == 1


def test_expired_token_reported_in_the_body_triggers_a_remint(tmp_path):
    """URA answers an expired token with HTTP 200 and the failure in the body,
    so checking the status code alone silently loses every batch."""
    expired = _FakeResponse(200, {
        "Status": "Error",
        "Message": "Token is valid for one day only. Your token exceed that. "
                   "Please try for new token to access the URA data service",
    })
    session = _FakeSession([
        expired,                                                  # batch rejected
        _FakeResponse(200),                                       # re-mint
        _FakeResponse(200, {"Status": "Success", "Result": [{"project": "X"}]}),
    ])
    cache = tmp_path / "t.json"
    cache.write_text(json.dumps({"token": "stale", "date": "2099-01-01"}))
    client = ura_mod.URAClient("key", session=session, token_cache=cache)
    client._token = "stale"

    assert client.fetch_batch(1) == [{"project": "X"}]
    assert session.calls == 3
    # The stale token is replaced, not merely dropped, so the next run starts
    # from a working one.
    assert json.loads(cache.read_text())["token"] == "tok-123"
    assert client._token == "tok-123"


def test_a_non_token_body_error_is_not_retried(tmp_path):
    """Only token failures should re-mint; anything else must surface."""
    session = _FakeSession([
        _FakeResponse(200, {"Status": "Error", "Message": "Invalid service name"}),
    ])
    client = ura_mod.URAClient("key", session=session, token_cache=tmp_path / "t.json")
    client._token = "tok"
    with pytest.raises(ura_mod.URAError, match="Invalid service name"):
        client.fetch_batch(1)
    assert session.calls == 1


def test_a_rejected_cached_token_is_reminted_once(tmp_path):
    """A token cached earlier today can still expire mid-run."""
    cache = tmp_path / "t.json"
    session = _FakeSession([
        _FakeResponse(403),                                   # batch rejected
        _FakeResponse(200),                                   # re-mint
        _FakeResponse(200, {"Status": "Success", "Result": []}),  # batch retry
    ])
    client = ura_mod.URAClient("key", session=session, token_cache=cache)
    client._token = "expired"
    assert client.fetch_batch(1) == []
    assert session.calls == 3


# --------------------------------------------------- data.gov.sg retries ----

class _DGResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {"success": True, "result": {}}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def test_datagov_retries_a_429(monkeypatch):
    """The schools pull runs straight after several HDB pulls and took a 429
    that failed a whole scheduled run — both clients share this retry now."""
    monkeypatch.setattr(datagov.time, "sleep", lambda s: None)
    session = _FakeSession([_DGResponse(429), _DGResponse(429), _DGResponse(200)])
    assert datagov.get({"resource_id": "x"}, session=session).status_code == 200
    assert session.calls == 3


def test_datagov_honours_retry_after(monkeypatch):
    waits = []
    monkeypatch.setattr(datagov.time, "sleep", waits.append)
    session = _FakeSession([_DGResponse(429, headers={"Retry-After": "7"}), _DGResponse(200)])
    datagov.get({"resource_id": "x"}, session=session)
    assert waits == [7.0]


def test_datagov_gives_up_after_max_retries(monkeypatch):
    monkeypatch.setattr(datagov.time, "sleep", lambda s: None)
    session = _FakeSession([_DGResponse(429)] * datagov.MAX_RETRIES)
    with pytest.raises(datagov.DataGovError, match="unreachable"):
        datagov.get({"resource_id": "x"}, session=session)


def test_school_directory_uses_the_retrying_client(monkeypatch):
    """Regression: schools.py had its own bare session.get and no backoff."""
    monkeypatch.setattr(datagov.time, "sleep", lambda s: None)
    page = {"success": True, "result": {"records": [
        {"school_name": "X PRIMARY SCHOOL", "postal_code": "123456",
         "mainlevel_code": "PRIMARY", "address": "1 RD", "dgp_code": "BISHAN",
         "zone_code": "NORTH", "url_address": ""}], "total": 1}}
    session = _FakeSession([_DGResponse(429), _DGResponse(200, page)])
    recs = schools_mod.fetch_directory(session=session)
    assert len(recs) == 1
    assert session.calls == 2


# ------------------------------------------------------- planning areas -----

def _square(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]


def test_point_in_polygon_basics():
    area = planning.PlanningArea("BOX", {"type": "Polygon", "coordinates": [
        _square(103.0, 1.0, 104.0, 2.0)]})
    assert area.contains(1.5, 103.5)        # (lat, lng) — inside
    assert not area.contains(1.5, 105.0)    # east of it
    assert not area.contains(3.0, 103.5)    # north of it


def test_polygon_holes_are_excluded():
    """A planning area with an enclave must not claim points inside it."""
    area = planning.PlanningArea("DONUT", {"type": "Polygon", "coordinates": [
        _square(0, 0, 10, 10), _square(4, 4, 6, 6)]})
    assert area.contains(1, 1)              # in the ring
    assert not area.contains(5, 5)          # in the hole


def test_multipolygon_matches_any_part():
    area = planning.PlanningArea("SPLIT", {"type": "MultiPolygon", "coordinates": [
        [_square(0, 0, 1, 1)], [_square(10, 10, 11, 11)]]})
    assert area.contains(0.5, 0.5)
    assert area.contains(10.5, 10.5)
    assert not area.contains(5, 5)


def test_bbox_rejects_far_points_without_scanning_rings():
    area = planning.PlanningArea("BOX", {"type": "Polygon", "coordinates": [
        _square(103.0, 1.0, 104.0, 2.0)]})
    assert area.bbox == (103.0, 1.0, 104.0, 2.0)
    assert not area.contains(50.0, 50.0)


def test_planning_load_falls_back_to_cache(tmp_path):
    """A OneMap outage must not stop a run — the committed cache stands in."""
    cache = tmp_path / "areas.json"
    cache.write_text(json.dumps([{
        "pln_area_n": "BISHAN",
        "geojson": json.dumps({"type": "Polygon",
                               "coordinates": [_square(103.8, 1.34, 103.86, 1.37)]}),
    }]))
    areas = planning.load(token=None, cache=cache)     # no token: no fetch
    assert set(areas) == {"BISHAN"}
    assert areas["BISHAN"].contains(1.35, 103.83)


def test_onemap_token_retries_a_400(monkeypatch, tmp_path):
    """OneMap answers 400 to rapid re-minting; that crashed a scheduled run."""
    monkeypatch.setattr(geocode_mod.time, "sleep", lambda s: None)

    class _Post:
        def __init__(self, statuses):
            self.statuses = list(statuses)
            self.calls = 0

        def post(self, url, **kwargs):
            self.calls += 1
            status = self.statuses.pop(0)
            payload = {"access_token": "tok", "expiry_timestamp": "9999999999"}
            return _DGResponse(status, payload if status == 200 else {})

    session = _Post([400, 400, 200])
    c = geocode_mod.OneMapClient("e@x", "pw", session=session,
                                 token_cache=tmp_path / "t.json")
    assert c.token() == "tok"
    assert session.calls == 3


def test_onemap_token_failure_message_points_at_throttling(monkeypatch, tmp_path):
    monkeypatch.setattr(geocode_mod.time, "sleep", lambda s: None)

    class _Post:
        def post(self, url, **kwargs):
            return _DGResponse(400, {})

    c = geocode_mod.OneMapClient("e@x", "pw", session=_Post(),
                                 token_cache=tmp_path / "t.json")
    with pytest.raises(geocode_mod.OneMapError, match="throttles"):
        c.token()


def test_planning_load_without_cache_or_token_is_empty(tmp_path):
    assert planning.load(token=None, cache=tmp_path / "missing.json") == {}


# ---------------------------------------------------- master plan land use ---

@pytest.fixture
def mp_features():
    return json.loads((FIXTURES / "masterplan.json").read_text())["features"]


@pytest.fixture
def mp_areas():
    """The real committed boundaries, so the clip is tested against the same
    polygons it runs against. Offline — these are cached in the repo."""
    areas = planning.load()
    return {name: areas[name] for name in ("TOA PAYOH", "BISHAN")}


def test_every_island_wide_land_use_has_a_bucket():
    """All 33 descriptions the live 2025 layer carries are mapped. A new one
    would fall to OTHER and be logged — this guards the ones we know about."""
    known = [
        "RESIDENTIAL", "ROAD", "COMMERCIAL", "BUSINESS 2",
        "RESIDENTIAL WITH COMMERCIAL AT 1ST STOREY", "PARK", "BUSINESS 1",
        "UTILITY", "WATERBODY", "RESIDENTIAL / INSTITUTION",
        "COMMERCIAL & RESIDENTIAL", "OPEN SPACE", "CIVIC & COMMUNITY INSTITUTION",
        "PLACE OF WORSHIP", "RESERVE SITE", "EDUCATIONAL INSTITUTION",
        "COMMERCIAL / INSTITUTION", "TRANSPORT FACILITIES", "HOTEL", "AGRICULTURE",
        "SPORTS & RECREATION", "HEALTH & MEDICAL CARE", "WHITE", "BUSINESS PARK",
        "SPECIAL USE", "MASS RAPID TRANSIT", "PORT / AIRPORT", "BUSINESS 1 - WHITE",
        "BEACH AREA", "BUSINESS 2 - WHITE", "CEMETERY", "LIGHT RAPID TRANSIT",
        "BUSINESS PARK - WHITE",
    ]
    assert len(known) == 33
    unmapped = [u for u in known if masterplan.bucket_of(u) == masterplan.OTHER]
    assert unmapped == []


def test_residential_is_the_ground_not_a_bucket():
    """Residential is 82% of parcels; it is drawn as ground, so it must not be
    one of the six categorical buckets competing for a colour."""
    assert masterplan.bucket_of("RESIDENTIAL") == masterplan.GROUND
    assert masterplan.GROUND not in [key for key, _, _ in masterplan.BUCKETS]
    assert len(masterplan.BUCKETS) == 6


def test_bucket_lookup_is_case_and_space_insensitive():
    assert masterplan.bucket_of("  business 1  ") == "business"
    assert masterplan.bucket_of("Park") == "green"


def test_an_unknown_land_use_is_kept_not_dropped(mp_features, mp_areas):
    """A future gazette can add a category. It still gets drawn — silently
    losing parcels would leave a hole that reads as missing data."""
    built = masterplan.build(mp_features, mp_areas)
    others = [f for f in built["features"] if f["properties"]["b"] == masterplan.OTHER]
    assert [f["properties"]["lu"] for f in others] == ["SOMETHING NEWLY GAZETTED"]


def test_unknown_land_uses_are_logged_by_name(mp_features, mp_areas, caplog):
    with caplog.at_level("WARNING", logger="ingest.masterplan"):
        masterplan.build(mp_features, mp_areas)
    assert "SOMETHING NEWLY GAZETTED" in caplog.text


def test_parcels_outside_the_area_are_clipped_away(mp_features, mp_areas):
    built = masterplan.build(mp_features, mp_areas)
    # The ROAD decoy sits at (5, 5), well outside the area.
    assert "ROAD" not in [f["properties"]["lu"] for f in built["features"]]


def test_a_parcel_that_merely_touches_the_area_is_not_dragged_in(mp_features, mp_areas):
    """The Central Catchment is one OPEN SPACE parcel 8.1 km across. Because it
    touches Bishan, a "keep it if any vertex is inside" clip pulled the whole
    thing onto the map — a green mass from Bukit Panjang to Thomson on an
    overlay that covers two planning areas. Caught by looking at the render,
    not the code. The fixture's straddler stands in for it: two vertices
    inside TOA PAYOH, centre outside."""
    built = masterplan.build(mp_features, mp_areas)
    straddlers = [f for f in built["features"]
                  if f["properties"]["lu"] == "RESIDENTIAL"
                  and f["properties"]["gpr"] == "LND"]
    assert straddlers == []


def test_representative_point_stays_on_a_concave_parcel():
    """An L-shaped parcel's centroid falls outside it, which would assign the
    parcel to whichever area that empty spot belongs to."""
    ell = {"type": "Polygon", "coordinates": [[
        [0, 0], [3, 0], [3, 1], [1, 1], [1, 3], [0, 3], [0, 0]]]}
    lng, lat = masterplan.representative_point(ell)
    assert masterplan._point_in_ring(lng, lat, ell["coordinates"][0])


def test_representative_point_uses_the_largest_part_of_a_multipolygon():
    """A parcel with a big part in one area and a speck in another belongs to
    the area holding the bulk of it."""
    geom = {"type": "MultiPolygon", "coordinates": [
        [_square(0, 0, 0.1, 0.1)],          # speck
        [_square(10, 10, 12, 12)],          # the bulk
    ]}
    lng, lat = masterplan.representative_point(geom)
    assert 10 < lng < 12 and 10 < lat < 12


def test_representative_point_of_an_empty_geometry_is_none():
    assert masterplan.representative_point({"type": "Polygon", "coordinates": []}) is None


def test_a_parcel_with_no_geometry_is_skipped(mp_features, mp_areas):
    built = masterplan.build(mp_features, mp_areas)
    assert "COMMERCIAL" not in [f["properties"]["lu"] for f in built["features"]]


def test_export_keeps_only_the_fields_the_map_draws(mp_features, mp_areas):
    """The source ships ten properties per parcel; at this many parcels the
    ones nobody draws cost more than the geometry."""
    built = masterplan.build(mp_features, mp_areas)
    assert {k for f in built["features"] for k in f["properties"]} == {"b", "lu", "gpr"}


def test_coordinates_are_rounded(mp_features, mp_areas):
    built = masterplan.build(mp_features, mp_areas, precision=6)
    first = built["features"][0]["geometry"]["coordinates"][0][0]
    assert first == [103.848, 1.332]   # from 103.84800000001 / 1.33200000002


def test_multipolygon_geometry_survives_the_round_trip(mp_features, mp_areas):
    built = masterplan.build(mp_features, mp_areas)
    park = next(f for f in built["features"] if f["properties"]["lu"] == "PARK")
    assert park["geometry"]["type"] == "MultiPolygon"
    assert park["geometry"]["coordinates"][0][0][0] == [103.856, 1.332]


def test_null_gpr_becomes_an_empty_string_not_the_word_none(mp_features, mp_areas):
    built = masterplan.build(mp_features, mp_areas)
    park = next(f for f in built["features"] if f["properties"]["lu"] == "PARK")
    assert park["properties"]["gpr"] == ""


def test_build_carries_the_legend_and_counts(mp_features, mp_areas):
    built = masterplan.build(mp_features, mp_areas)
    assert built["areas"] == ["BISHAN", "TOA PAYOH"]
    assert built["counts"]["homes"] == 1
    assert built["counts"]["business"] == 1
    assert built["counts"]["civic"] == 1      # the Bishan parcel — both areas clip
    # The legend drives the frontend's swatch list, so every bucket drawn must
    # have a label to put beside it.
    labelled = {row["key"] for row in built["legend"]}
    assert set(built["counts"]) <= labelled


def test_build_without_an_area_refuses_rather_than_exporting_the_island(mp_features):
    with pytest.raises(masterplan.MasterPlanError):
        masterplan.build(mp_features, {})


def test_gpr_codes_cover_the_non_numeric_values_the_plan_uses():
    assert set(masterplan.GPR_CODES) == {"LND", "EVA", "SDP"}


def test_export_masterplan_writes_compact_json(store, mp_features, mp_areas, tmp_path):
    out = tmp_path / "masterplan.json"
    store.export_masterplan(masterplan.build(mp_features, mp_areas), out)
    body = out.read_text()
    assert '", "' not in body                 # separators=(",", ":")
    payload = json.loads(body)
    assert payload["generated_at"].endswith("Z")
    assert len(payload["features"]) == 5


def test_a_failed_pull_leaves_the_last_good_overlay_alone(store, tmp_path):
    """Same rule as the schools export: an empty result must not blank a file
    that is still perfectly good."""
    out = tmp_path / "masterplan.json"
    out.write_text('{"features": ["previous"]}')
    store.export_masterplan(None, out)
    store.export_masterplan({"features": []}, out)
    assert json.loads(out.read_text())["features"] == ["previous"]


def test_download_url_reads_the_poll_response(monkeypatch):
    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"code": 0, "data": {"url": "https://example/x.geojson"}}

    class _S:
        def get(self, url, **kw): return _R()

    assert masterplan.download_url(_S()) == "https://example/x.geojson"


def test_download_url_without_a_url_raises_rather_than_returning_none(monkeypatch):
    class _R:
        def raise_for_status(self): pass
        def json(self): return {"code": 1, "data": {}, "errorMsg": "nope"}

    class _S:
        def get(self, url, **kw): return _R()

    with pytest.raises(masterplan.MasterPlanError):
        masterplan.download_url(_S())


def test_watchlist_areas_are_deduped_in_order():
    wl = {
        "private": [
            {"planning_area": "TOA PAYOH"},
            {"project": "TREVISTA"},
            {"planning_area": "BISHAN"},
            {"planning_area": "TOA PAYOH"},
        ],
        "hdb": [],
    }
    assert watchlist_areas(wl) == ["TOA PAYOH", "BISHAN"]


def test_an_hdb_town_extends_the_overlay_to_its_planning_area():
    """Deriving the clip from private entries alone left an HDB town in a new
    area with no parcels under it, and nothing in the log to say why."""
    wl = {
        "private": [{"planning_area": "BISHAN"}],
        "hdb": [{"town": "TAMPINES"}, {"town": "BISHAN"}],
    }
    assert watchlist_areas(wl, known={"BISHAN", "TAMPINES"}) == ["BISHAN", "TAMPINES"]


def test_an_hdb_town_that_is_not_a_planning_area_is_skipped_and_named(caplog):
    """KALLANG/WHAMPOA and CENTRAL AREA each straddle several planning areas;
    picking one would quietly overlay the wrong ground."""
    wl = {"private": [], "hdb": [{"town": "KALLANG/WHAMPOA"}, {"town": "BISHAN"}]}
    with caplog.at_level("WARNING", logger="ingest"):
        assert watchlist_areas(wl, known={"BISHAN"}) == ["BISHAN"]
    assert "KALLANG/WHAMPOA" in caplog.text


def test_the_real_watchlist_covers_every_town_it_watches():
    """Guards the whole point of the above: every area the watchlist names,
    from either source, resolves to a boundary the overlay can clip to."""
    wl = load_watchlist()
    known = set(planning.load())
    areas = watchlist_areas(wl, known=known)
    assert "TAMPINES" in areas and "TOA PAYOH" in areas and "BISHAN" in areas
    assert set(areas) <= known


def test_no_planning_area_means_no_overlay_rather_than_the_whole_island():
    """The overlay is defined by the clip. With nothing to clip to, exporting
    all 113,394 island-wide parcels would be the wrong kind of "helpful"."""
    wl = {"private": [{"project": "TREVISTA"}], "hdb": []}
    assert collect_masterplan(wl, from_fixtures=True) is None


def test_a_master_plan_failure_is_collected_not_raised(monkeypatch):
    def boom():
        raise RuntimeError("data.gov.sg is down")
    monkeypatch.setattr(masterplan, "fetch", boom)

    errors = []
    got = collect_masterplan({"private": [{"planning_area": "BISHAN"}], "hdb": []},
                             False, errors)
    assert got is None
    assert errors and "data.gov.sg is down" in errors[0]


# ----------------------------------------------------------------- HDB ------

def test_street_filter_narrows_to_one_street(hdb_records):
    subset = [r for r in hdb_records
              if r["town"] == "TOA PAYOH" and r["flat_type"] == "5 ROOM"]
    narrowed = hdb_mod.filter_records(subset, street_name="LORONG 1A TOA PAYOH")
    assert narrowed and len(narrowed) < len(subset)
    assert {r["street_name"] for r in narrowed} == {"LOR 1A TOA PAYOH"}


def test_filters_are_case_insensitive(hdb_records):
    a = hdb_mod.filter_records(hdb_records, street_name="lorong 1a toa payoh")
    b = hdb_mod.filter_records(hdb_records, street_name="LORONG 1A TOA PAYOH")
    assert len(a) == len(b) > 0


def test_block_filter_narrows_further(hdb_records):
    street = hdb_mod.filter_records(hdb_records, street_name="LORONG 1A TOA PAYOH")
    block = hdb_mod.filter_records(street, block="138A")
    assert block and len(block) < len(street)
    assert {r["block"] for r in block} == {"138A"}


def test_spelled_out_street_matches_the_datasets_abbreviation(hdb_records):
    """The dataset says "LOR 1A TOA PAYOH"; nobody writes that in a watchlist."""
    spelled = hdb_mod.filter_records(hdb_records, street_name="LORONG 1A TOA PAYOH")
    abbrev = hdb_mod.filter_records(hdb_records, street_name="LOR 1A TOA PAYOH")
    assert len(spelled) == len(abbrev) > 0


@pytest.mark.parametrize(
    "written,dataset",
    [
        ("LORONG 1A TOA PAYOH", "LOR 1A TOA PAYOH"),
        ("BISHAN STREET 13", "BISHAN ST 13"),
        ("ANG MO KIO AVENUE 3", "ANG MO KIO AVE 3"),
        ("UPPER SERANGOON ROAD", "UPP SERANGOON RD"),
        ("JALAN BUKIT MERAH", "JLN BT MERAH"),
        ("TOA PAYOH NORTH", "TOA PAYOH NTH"),
    ],
)
def test_street_abbreviations_canonicalize_to_the_same_key(written, dataset):
    assert hdb_mod.canonical_street(written) == hdb_mod.canonical_street(dataset)


def test_street_filter_still_rejects_a_genuinely_different_street(hdb_records):
    assert hdb_mod.filter_records(hdb_records, street_name="LOR 2 TOA PAYOH") == []


def test_flat_model_filter(hdb_records):
    """"Executive maisonette" is flat_type EXECUTIVE + flat_model Maisonette —
    there is no EXECUTIVE MAISONETTE flat_type in the dataset."""
    recs = [
        {"flat_type": "EXECUTIVE", "flat_model": "Maisonette", "lease_commence_date": "1988"},
        {"flat_type": "EXECUTIVE", "flat_model": "Apartment", "lease_commence_date": "1993"},
    ]
    assert len(hdb_mod.filter_records(recs, flat_model="Maisonette")) == 1
    assert len(hdb_mod.filter_records(recs, flat_model="MAISONETTE")) == 1   # case-insensitive
    assert len(hdb_mod.filter_records(recs, flat_model="Apartment")) == 1
    assert hdb_mod.filter_records(recs, flat_model="DBSS") == []
    assert len(hdb_mod.filter_records(recs)) == 2                            # unset = no filter


@pytest.mark.parametrize(
    "lease_from,expected",
    [(None, 4), (2000, 2), (2012, 1), (1980, 4), (2030, 0)],
)
def test_lease_from_filter(lease_from, expected):
    recs = [{"lease_commence_date": y} for y in ("1985", "1999", "2001", "2012")]
    assert len(hdb_mod.filter_records(recs, lease_from=lease_from)) == expected


def test_unparseable_lease_year_fails_a_lease_bound():
    """It can't be shown to meet the bound, so it must not pass it."""
    recs = [{"lease_commence_date": ""}, {"lease_commence_date": "n/a"}, {}]
    assert hdb_mod.filter_records(recs, lease_from=2000) == []
    assert len(hdb_mod.filter_records(recs)) == 3      # but survives with no bound


def test_filters_compose(hdb_records):
    both = hdb_mod.filter_records(
        hdb_records, street_name="LORONG 1A TOA PAYOH", lease_from=1900)
    street_only = hdb_mod.filter_records(hdb_records, street_name="LORONG 1A TOA PAYOH")
    assert len(both) == len(street_only) > 0
    assert hdb_mod.filter_records(
        hdb_records, street_name="LORONG 1A TOA PAYOH", lease_from=2500) == []


def test_hdb_property_name_is_block_plus_street(hdb_records):
    txns = hdb_mod.normalize(
        hdb_mod.filter_records(hdb_records, street_name="LORONG 1A TOA PAYOH"))
    t = txns[0]
    assert t.source == SOURCE_HDB
    assert t.property_name.endswith("LOR 1A TOA PAYOH")
    assert t.district_town == "TOA PAYOH"
    assert t.property_type == "5 ROOM"


# --------------------------------------------------------------- store ------

def _sample(hdb_records):
    return hdb_mod.normalize(
        hdb_mod.filter_records(hdb_records, street_name="LORONG 1A TOA PAYOH"))


def test_upsert_is_idempotent(store, hdb_records):
    txns = _sample(hdb_records)
    added, updated = store.upsert_many(txns)
    assert added == len(txns) and updated == 0
    first_count = store.count()

    added2, updated2 = store.upsert_many(txns)      # same data, second run
    assert added2 == 0
    assert updated2 == len(txns)
    assert store.count() == first_count             # no duplicates


def test_three_runs_still_no_duplicates(store, hdb_records):
    txns = _sample(hdb_records)
    for _ in range(3):
        store.upsert_many(txns)
    assert store.count() == len(txns)


def test_duplicates_within_one_batch_collapse(store, hdb_records):
    txns = _sample(hdb_records)
    added, _ = store.upsert_many(txns + txns)
    assert added == len(txns)
    assert store.count() == len(txns)


def test_rows_that_gain_coordinates_later_do_not_duplicate(store, ura_projects):
    """URA rows arrive with no coordinates and are geocoded afterwards. If the
    dedup key included lat/lng, the next run would re-insert every one."""
    txns = ura_mod.normalize(ura_projects, ["TREVISTA"], svy21_to_wgs84)
    for t in txns:
        t.lat = t.lng = None
    added, _ = store.upsert_many(txns)
    assert added == len(txns)

    for t in txns:                       # geocoded on a later pass
        t.lat, t.lng = 1.33509, 103.84662
    added2, updated2 = store.upsert_many(txns)
    assert added2 == 0
    assert updated2 == len(txns)
    assert store.count() == len(txns)
    assert all(r["lat"] == 1.33509 for r in store.all_rows())


def test_rows_survive_falling_out_of_uras_5_year_window(store, tmp_path):
    """The whole reason the database is committed rather than regenerated.

    URA serves a rolling 5 years and drops anything older. A later run simply
    won't mention those transactions — and because the store only inserts and
    updates, never deletes, they stay in the archive and stay on the map."""
    old = ura_mod.normalize([{
        "project": "TREVISTA", "street": "LORONG 3 TOA PAYOH", "marketSegment": "RCR",
        "transaction": [{"contractDate": "0619", "price": "1500000", "area": "100",
                         "propertyType": "Condominium", "district": "12",
                         "floorRange": "06-10", "tenure": "99 yrs lease commencing from 2008"}],
    }], ["TREVISTA"], svy21_to_wgs84)
    store.upsert_many(old)
    assert store.count() == 1

    # A later pull, years on: URA no longer returns the 2019 caveat at all.
    recent = ura_mod.normalize([{
        "project": "TREVISTA", "street": "LORONG 3 TOA PAYOH", "marketSegment": "RCR",
        "transaction": [{"contractDate": "0326", "price": "2400000", "area": "100",
                         "propertyType": "Condominium", "district": "12",
                         "floorRange": "06-10", "tenure": "99 yrs lease commencing from 2008"}],
    }], ["TREVISTA"], svy21_to_wgs84)
    added, _ = store.upsert_many(recent)

    assert added == 1
    assert store.count() == 2                      # the 2019 row was not touched
    dates = sorted(r["txn_date"] for r in store.all_rows())
    assert dates == ["2019-06-01", "2026-03-01"]

    # And it still reaches the map, not just the database.
    payload = store.export_json(tmp_path / "d.json")
    exported = [t["date"] for p in payload["properties"] for t in p["txns"]]
    assert "2019-06-01" in exported


def test_ura_and_hdb_rows_coexist(store, hdb_records, ura_projects):
    store.upsert_many(_sample(hdb_records))
    store.upsert_many(ura_mod.normalize(ura_projects, ["TREVISTA"], svy21_to_wgs84))
    sources = {r["source"] for r in store.all_rows()}
    assert sources == {SOURCE_URA, SOURCE_HDB}


def test_late_geocode_backfills_existing_rows(store, hdb_records):
    txns = _sample(hdb_records)
    store.upsert_many(txns)
    name = txns[0].property_name
    n = store.backfill_coords(name, SOURCE_HDB, 1.3385, 103.8455)
    assert n > 0
    rows = [r for r in store.all_rows() if r["property_name"] == name]
    assert all(r["lat"] == 1.3385 for r in rows)


def test_upsert_never_nulls_a_known_coordinate(store, hdb_records):
    txns = _sample(hdb_records)
    for t in txns:
        t.lat, t.lng = 1.3385, 103.8455
    store.upsert_many(txns)
    for t in txns:                      # a later run without a geocode
        t.lat = t.lng = None
    store.upsert_many(txns)
    assert all(r["lat"] == 1.3385 for r in store.all_rows())


# -------------------------------------------------------------- export ------

def test_one_block_with_two_flat_types_stays_two_properties(store, tmp_path):
    """8 Joo Seng Rd really does hold both 5 ROOM and EXECUTIVE units. Grouping
    the export on name alone merged them into one marker whose type and psf
    came from whichever row happened to sort first."""
    base = {
        "month": "2025-03", "town": "TOA PAYOH", "block": "8",
        "street_name": "JOO SENG RD", "storey_range": "04 TO 06",
        "flat_model": "Improved", "lease_commence_date": "1983",
        "remaining_lease": "57 years",
    }
    recs = [
        {**base, "flat_type": "5 ROOM", "floor_area_sqm": "120", "resale_price": "700000"},
        {**base, "flat_type": "EXECUTIVE", "floor_area_sqm": "146", "resale_price": "900000"},
    ]
    store.upsert_many(hdb_mod.normalize(recs))
    payload = store.export_json(tmp_path / "d.json")

    assert len(payload["properties"]) == 2
    assert {p["type"] for p in payload["properties"]} == {"5 ROOM", "EXECUTIVE"}
    assert len({p["id"] for p in payload["properties"]}) == 2   # ids stay distinct


def test_one_block_with_two_flat_models_stays_two_properties(store, tmp_path):
    """236 Lor 1 Toa Payoh holds executive maisonettes AND apartments — very
    different products (~166 vs ~142 sqm). Merging them would report a blended
    psf under whichever model label sorted last."""
    base = {
        "month": "2025-03", "town": "TOA PAYOH", "block": "236",
        "street_name": "LOR 1 TOA PAYOH", "flat_type": "EXECUTIVE",
        "storey_range": "04 TO 06", "lease_commence_date": "1988",
        "remaining_lease": "62 years",
    }
    recs = [
        {**base, "flat_model": "Maisonette", "floor_area_sqm": "166", "resale_price": "1100000"},
        {**base, "flat_model": "Apartment", "floor_area_sqm": "142", "resale_price": "900000"},
    ]
    store.upsert_many(hdb_mod.normalize(recs))
    payload = store.export_json(tmp_path / "d.json")

    assert len(payload["properties"]) == 2
    assert {p["flat_model"] for p in payload["properties"]} == {"Maisonette", "Apartment"}
    assert len({p["id"] for p in payload["properties"]}) == 2
    # Each keeps its own psf rather than a blend of the two.
    psfs = {p["flat_model"]: p["latest_psf"] for p in payload["properties"]}
    assert psfs["Maisonette"] != psfs["Apartment"]


def test_flat_model_is_stored_and_exported(store, hdb_records, tmp_path):
    txns = hdb_mod.normalize(
        hdb_mod.filter_records(hdb_records, street_name="LOR 1A TOA PAYOH"))
    assert all(t.flat_model for t in txns)
    store.upsert_many(txns)
    assert all(r["flat_model"] for r in store.all_rows())


def test_migration_adds_flat_model_to_an_older_database(tmp_path):
    """The db is committed, so CI checks out one written by an earlier version.
    CREATE TABLE IF NOT EXISTS won't add a column to an existing table."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL, property_name TEXT NOT NULL, property_type TEXT,
            segment TEXT, address TEXT, district_town TEXT, txn_date TEXT NOT NULL,
            price REAL NOT NULL, area_sqm REAL, area_sqft REAL, price_psf REAL,
            storey_range TEXT, tenure TEXT, lat REAL, lng REAL, raw_json TEXT,
            first_seen TEXT, last_seen TEXT
        );
        """
    )
    con.commit()
    con.close()

    with Store(path) as s:                       # must not raise
        cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(transactions)")}
        assert "flat_model" in cols
        s.upsert_many(hdb_mod.normalize([{
            "month": "2025-01", "town": "TOA PAYOH", "block": "1",
            "street_name": "LOR 1 TOA PAYOH", "flat_type": "EXECUTIVE",
            "flat_model": "Maisonette", "storey_range": "01 TO 03",
            "floor_area_sqm": "146", "resale_price": "1000000",
            "lease_commence_date": "1988", "remaining_lease": "62 years",
        }]))
        assert s.all_rows()[0]["flat_model"] == "Maisonette"


def test_export_json_shape(store, hdb_records, ura_projects, tmp_path):
    store.upsert_many(_sample(hdb_records))
    store.upsert_many(ura_mod.normalize(ura_projects, ["TREVISTA"], svy21_to_wgs84))
    payload = store.export_json(tmp_path / "data.json")

    assert payload["generated_at"].endswith("Z")
    assert payload["properties"]
    for prop in payload["properties"]:
        assert {"id", "name", "source", "lat", "lng", "latest_psf", "txns"} <= set(prop)
        assert prop["txn_count"] == len(prop["txns"])
        dates = [t["date"] for t in prop["txns"]]
        assert dates == sorted(dates)               # chart expects date order


def test_export_csv_has_a_row_per_transaction(store, hdb_records, tmp_path):
    txns = _sample(hdb_records)
    store.upsert_many(txns)
    out = store.export_csv(tmp_path)
    lines = out.read_text().strip().splitlines()
    assert len(lines) == len(txns) + 1              # + header


def test_slugify():
    assert slugify("Sky Habitat") == "sky-habitat"
    assert slugify("138A LOR 1A TOA PAYOH") == "138a-lor-1a-toa-payoh"


# ------------------------------------------------------------ geocoder ------

class _CountingClient:
    def __init__(self, result=(1.3385, 103.8455)):
        self.result = result
        self.calls = 0

    def search(self, address):
        self.calls += 1
        return self.result


def test_geocode_cache_prevents_a_second_lookup(store):
    client = _CountingClient()
    geo = Geocoder(store, client)
    assert geo.lookup("138A LOR 1A TOA PAYOH") == (1.3385, 103.8455)
    assert geo.lookup("138A LOR 1A TOA PAYOH") == (1.3385, 103.8455)
    assert client.calls == 1                        # second came from SQLite


def test_geocode_failure_returns_none_without_raising(store):
    class Failing:
        def search(self, address):
            raise RuntimeError("onemap down")

    assert Geocoder(store, Failing()).lookup("nowhere") is None


def test_first_latlng_reads_onemap_shape():
    payload = json.loads((FIXTURES / "onemap_search.json").read_text())
    lat, lng = first_latlng(payload["138A LOR 1A TOA PAYOH"])
    assert 1.20 < lat < 1.48 and 103.6 < lng < 104.1


def test_first_latlng_on_empty_results():
    assert first_latlng({"found": 0, "results": []}) is None


# ----------------------------------------------------------- watchlist ------

def test_watchlist_loader_normalizes_and_forgives(tmp_path):
    path = tmp_path / "w.yaml"
    path.write_text(
        """
private:
  - project: "  trevista  "
  - project: ""
hdb:
  - town: " toa payoh "
    flat_type: "5 room"
    street_name: "lorong 1a toa payoh"
  - town: "QUEENSTOWN"
  - "not a mapping"
"""
    )
    wl = load_watchlist(path)
    assert wl["private"] == [                                # trimmed, empty dropped
        {"project": "trevista", "top_year": None}
    ]
    assert len(wl["hdb"]) == 1                               # incomplete entries dropped
    assert wl["hdb"][0]["town"] == "TOA PAYOH"               # uppercased for the API
    assert wl["hdb"][0]["flat_type"] == "5 ROOM"
    assert wl["hdb"][0]["street_name"] == "LORONG 1A TOA PAYOH"


def test_watchlist_reads_flat_model_and_lease_from(tmp_path):
    path = tmp_path / "w.yaml"
    path.write_text(
        """
hdb:
  - town: "TOA PAYOH"
    flat_type: "EXECUTIVE"
    flat_model: "Maisonette"
    lease_from: 2000
  - town: "TOA PAYOH"
    flat_type: "5 ROOM"
    lease_from: "not a year"
"""
    )
    wl = load_watchlist(path)
    assert wl["hdb"][0]["flat_model"] == "MAISONETTE"
    assert wl["hdb"][0]["lease_from"] == 2000
    assert wl["hdb"][1]["lease_from"] is None      # junk degrades to "no bound"
    assert wl["hdb"][1]["flat_model"] == ""


def test_missing_watchlist_does_not_crash(tmp_path):
    assert load_watchlist(tmp_path / "nope.yaml") == {"private": [], "hdb": []}


def test_real_watchlist_loads():
    wl = load_watchlist(Path(__file__).parent.parent / "config" / "watchlist.yaml")
    assert any("TREVISTA" in e["project"].upper() for e in wl["private"])
    assert any(e["town"] == "TOA PAYOH" for e in wl["hdb"])
