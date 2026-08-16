"""Rental ingestion — offline, against fixtures captured from the live APIs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ingest import rental
from ingest.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def hdb_records() -> list[dict]:
    payload = json.loads((FIXTURES / "hdb_rental_toa_payoh.json").read_text())
    return payload["result"]["records"]


def ura_projects() -> list[dict]:
    return json.loads((FIXTURES / "ura_rental_median.json").read_text())["Result"]


# --- the format trap -------------------------------------------------------

def test_rental_flat_type_uses_a_hyphen_where_resale_uses_a_space():
    """The whole reason canonical_flat_type exists. If this ever passes
    trivially it means the fixture stopped reflecting the real dataset."""
    raw = {r["flat_type"] for r in hdb_records()}
    assert any("-" in v for v in raw), "fixture no longer shows the hyphen form"
    assert rental.canonical_flat_type("5-ROOM") == "5 ROOM"


@pytest.mark.parametrize(
    "raw,expected",
    [("5-ROOM", "5 ROOM"), ("5 ROOM", "5 ROOM"), ("EXECUTIVE", "EXECUTIVE"),
     ("  3-room ", "3 ROOM"), (None, "")],
)
def test_canonical_flat_type(raw, expected):
    assert rental.canonical_flat_type(raw) == expected


def test_hdb_series_matches_resale_spelling():
    """Keyed on the canonical `5 ROOM`, so it joins to the transactions table."""
    areas = {("10 JOO SENG RD", "5 ROOM"): 1200.0}
    series = rental.hdb_series(hdb_records(), areas)
    assert ("10 JOO SENG RD", "5 ROOM") in series
    assert series[("10 JOO SENG RD", "5 ROOM")]


def test_hdb_series_skips_blocks_with_no_known_area():
    """No floor area means no defensible psf — better absent than invented."""
    assert rental.hdb_series(hdb_records(), {}) == {}


def test_hdb_series_converts_rent_to_psf_per_month():
    areas = {("10 JOO SENG RD", "5 ROOM"): 1200.0}
    series = rental.hdb_series(hdb_records(), areas)[("10 JOO SENG RD", "5 ROOM")]
    rents = [float(r["monthly_rent"]) for r in hdb_records()]
    assert min(series, key=lambda p: p[1])[1] == pytest.approx(min(rents) / 1200.0)
    # Sanity: Singapore HDB rents land in single-digit psf, not hundreds.
    assert all(0.5 < psf < 12 for _, psf in series)


def test_hdb_series_dates_become_month_starts():
    areas = {("10 JOO SENG RD", "5 ROOM"): 1200.0}
    series = rental.hdb_series(hdb_records(), areas)[("10 JOO SENG RD", "5 ROOM")]
    assert all(month.endswith("-01") and len(month) == 10 for month, _ in series)


def test_hdb_series_ignores_unparseable_rows():
    areas = {("1 SOME RD", "5 ROOM"): 1000.0}
    bad = [
        {"block": "1", "street_name": "SOME RD", "flat_type": "5-ROOM",
         "rent_approval_date": "2024-01", "monthly_rent": "not a number"},
        {"block": "1", "street_name": "SOME RD", "flat_type": "5-ROOM",
         "rent_approval_date": "2024-01", "monthly_rent": "0"},
        {"block": "1", "street_name": "SOME RD", "flat_type": "5-ROOM",
         "monthly_rent": "3000"},  # no date
    ]
    assert rental.hdb_series(bad, areas) == {}


# --- URA -------------------------------------------------------------------

@pytest.mark.parametrize(
    "ref,expected",
    [("2024Q1", "2024-01-01"), ("2024Q2", "2024-04-01"),
     ("2024Q3", "2024-07-01"), ("2024Q4", "2024-10-01"),
     ("2024Q5", None), ("nonsense", None), ("", None), (None, None)],
)
def test_quarter_to_month(ref, expected):
    assert rental.quarter_to_month(ref) == expected


def test_ura_series_keyed_by_upper_project_name():
    series = rental.ura_series(ura_projects())
    assert "TREVISTA" in series
    assert all(k == k.upper() for k in series)


def test_ura_median_is_already_psf_per_month():
    """URA documents `median` as psf/month — no conversion is applied, so this
    guards against someone later 'fixing' it by dividing by an area."""
    series = rental.ura_series(ura_projects())["TREVISTA"]
    assert all(2 < psf < 15 for _, psf in series), series


def test_ura_series_drops_entries_with_no_median():
    projects = [{"project": "GHOST", "rentalMedian": [
        {"refPeriod": "2024Q1", "median": None},
        {"refPeriod": "2024Q2", "median": "-"},
        {"refPeriod": "bad", "median": "5.0"},
    ]}]
    assert rental.ura_series(projects) == {}


# --- store round-trip ------------------------------------------------------

def test_upsert_rentals_is_idempotent(tmp_path):
    rows = [("HDB", "10 JOO SENG RD", "5 ROOM", "2024-01-01", 2.5, 3)]
    with Store(tmp_path / "t.db") as store:
        store.upsert_rentals(rows)
        store.upsert_rentals(rows)
        n = store.conn.execute("SELECT COUNT(*) c FROM rentals").fetchone()["c"]
    assert n == 1, "re-running a pull must not duplicate rental rows"


def test_upsert_rentals_updates_in_place(tmp_path):
    with Store(tmp_path / "t.db") as store:
        store.upsert_rentals([("HDB", "A", "5 ROOM", "2024-01-01", 2.5, 3)])
        store.upsert_rentals([("HDB", "A", "5 ROOM", "2024-01-01", 3.1, 9)])
        row = store.conn.execute("SELECT * FROM rentals").fetchone()
    assert (row["rent_psf"], row["contracts"]) == (3.1, 9)


def test_median_areas_sqft_only_reads_hdb(tmp_path):
    """URA rents arrive as psf already; pulling a URA area in here would mean
    somebody is about to divide a psf figure by an area a second time."""
    with Store(tmp_path / "t.db") as store:
        store.conn.execute(
            "INSERT INTO transactions (dedup_key, source, property_name, "
            "property_type, txn_date, price, area_sqft) "
            "VALUES ('k1','URA','SOME CONDO','Condominium','2024-01-01',1e6,1000)")
        store.conn.commit()
        assert store.median_areas_sqft() == {}
