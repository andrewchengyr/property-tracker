"""MOE P1 balloting — offline, against a fixture cut from the real page."""

from __future__ import annotations

from pathlib import Path

import pytest

from ingest import ballot
from ingest.store import Store

FIXTURES = Path(__file__).parent / "fixtures"


def html() -> str:
    return (FIXTURES / "moe_ballot.html").read_text()


def rows() -> list[dict]:
    return ballot.parse(html())


def row_for(name_fragment: str, phase: str) -> dict:
    for r in rows():
        if name_fragment.lower() in r["school_name"].lower() and r["phase"] == phase:
            return r
    raise AssertionError(f"no fixture row for {name_fragment} {phase}")


# --- parsing the embedded payload ------------------------------------------

def test_parses_the_flight_payload():
    got = rows()
    assert got, "nothing parsed — the fixture or the extractor is broken"
    assert {r["year"] for r in got} == {"2025"}


def test_every_row_has_a_school_year_and_phase():
    assert all(r["school_key"] and r["year"] and r["phase"] for r in rows())


def test_missing_schooldata_raises_rather_than_returning_empty():
    """A silent empty list would look exactly like 'MOE published nothing this
    year' and would quietly stop refreshing the archive."""
    with pytest.raises(ballot.BallotError):
        ballot.parse("<html><body>no payload here</body></html>")


# --- the join --------------------------------------------------------------

def test_through_train_primary_suffix_is_stripped():
    """MOE writes `Catholic High School (Primary)`; the school directory calls
    it `CATHOLIC HIGH SCHOOL`. Fourth field in this project where two official
    sources name the same thing differently."""
    assert ballot.normalise_name("Catholic High School (Primary)") == "CATHOLIC HIGH SCHOOL"
    assert ballot.normalise_name("CHIJ St. Nicholas Girls' School (Primary)") == \
        "CHIJ ST NICHOLAS GIRLS SCHOOL"


def test_st_andrew_needs_an_explicit_alias():
    """The only pair that normalising alone cannot reconcile: MOE says
    `St Andrew's Junior School`, the directory says `ST ANDREW'S SCHOOL
    (JUNIOR)`."""
    assert ballot.normalise_name("St Andrew's Junior School") == \
        ballot.normalise_name("ST ANDREW'S SCHOOL (JUNIOR)")


def test_punctuation_differences_do_not_break_the_join():
    assert ballot.normalise_name("CHIJ St. Nicholas Girls' School") == \
        ballot.normalise_name("CHIJ ST. NICHOLAS GIRLS' SCHOOL")


# --- the distance-band reading, which is the whole feature -----------------

def test_balloting_within_1km_means_further_bands_got_nothing():
    """The cut-off is not a flag. A school that balloted inside 1 km is the
    *worst* case for a 1-2 km buyer — everything beyond the cut-off missed out
    entirely. Reading it as three independent yes/nos inverts the meaning."""
    out = ballot.band_outcomes(row_for("Anglo-Chinese School (Junior)", "2C"))
    assert out == {"within_1km": "ballot", "1_2km": "none", "outside_2km": "none"}


def test_balloting_between_1_and_2km_means_inside_1km_walked_in():
    out = ballot.band_outcomes(row_for("Alexandra", "2C"))
    assert out == {"within_1km": "in", "1_2km": "ballot", "outside_2km": "none"}


def test_balloting_outside_2km_means_both_nearer_bands_walked_in():
    out = ballot.band_outcomes(row_for("Nicholas", "2A"))
    assert out == {"within_1km": "in", "1_2km": "in", "outside_2km": "ballot"}


def test_no_balloting_for_all_citizens_admits_every_band():
    out = ballot.band_outcomes(row_for("Greenwood", "2CS"))
    assert out == {b: "in" for b in ballot.BANDS}


def test_places_offered_only_within_2km_leaves_the_far_band_out():
    row = {"cutoff_band": "within_2km", "balloted": False}
    assert ballot.band_outcomes(row) == {
        "within_1km": "in", "1_2km": "in", "outside_2km": "none"}


def test_an_unrecognised_note_yields_no_verdict_rather_than_a_guess():
    """One 2025 note explains a PR intake cap and names no distance at all.
    Inventing a band for it would put a fabricated verdict on screen."""
    row = row_for("Bukit Timah Primary", "2C")
    assert "cap on the intake" in row["note"]
    assert row["cutoff_band"] is None
    assert ballot.band_outcomes(row) is None


def test_classify_returns_nothing_for_an_empty_note():
    assert ballot.classify("") == (None, None)


def test_cohort_is_read_from_the_sentence():
    band, who = ballot.classify(
        "Conducted for: Permanent Resident children residing within 1km of the school.")
    assert (band, who) == ("within_1km", "PR")
    band, who = ballot.classify(
        "Conducted for: Singapore Citizen children residing within 1km of the school.")
    assert (band, who) == ("within_1km", "SC")


def test_html_is_stripped_from_notes():
    assert "<" not in row_for("Bukit Timah Primary", "2C")["note"]


# --- store -----------------------------------------------------------------

def test_upsert_ballot_is_idempotent(tmp_path):
    with Store(tmp_path / "t.db") as store:
        store.upsert_ballot(rows())
        store.upsert_ballot(rows())
        n = store.conn.execute("SELECT COUNT(*) c FROM p1_ballot").fetchone()["c"]
    assert n == len(rows())


def test_upsert_ballot_updates_in_place(tmp_path):
    """MOE corrects this data after publication, so a re-pull must overwrite."""
    base = dict(rows()[0])
    with Store(tmp_path / "t.db") as store:
        store.upsert_ballot([base])
        store.upsert_ballot([{**base, "applicants": 999}])
        got = store.conn.execute("SELECT applicants FROM p1_ballot").fetchone()
    assert got["applicants"] == 999


def test_archived_years_survive_a_failed_pull(tmp_path):
    """MOE publishes one exercise and replaces it; the archive is the only copy
    of anything older. A year that can no longer be fetched must still export."""
    old = [{**r, "year": "2024"} for r in rows()]
    with Store(tmp_path / "t.db") as store:
        store.upsert_ballot(old)
        store.upsert_ballot(rows())            # this year's pull
        assert store.ballot_years() == {"2024", "2025"}
        history = store.ballot_by_school()
    any_school = next(iter(history.values()))
    assert {r["year"] for r in any_school} == {"2024", "2025"}


def test_an_undersubscribed_phase_admits_every_band_without_a_note():
    """MOE leaves the sentence blank when a phase was never contested — most
    phases. Fewer applicants than vacancies and no ballot means everyone was
    placed whatever their distance, which beats showing an empty row."""
    assert ballot.band_outcomes(
        {"cutoff_band": None, "balloted": False, "vacancies": 134, "applicants": 104}
    ) == {b: "in" for b in ballot.BANDS}


def test_an_oversubscribed_phase_with_no_note_stays_unknown():
    """Applicants over vacancies and yet no balloting recorded is a shape we
    have not seen; guessing a band there would be inventing the answer."""
    assert ballot.band_outcomes(
        {"cutoff_band": None, "balloted": False, "vacancies": 10, "applicants": 99}
    ) is None


def test_a_phase_with_no_vacancies_is_not_reported_as_easy():
    """Zero vacancies means the phase was not conducted. Treating 0 applicants
    against 0 vacancies as "everyone was placed" would tell a reader the phase
    was open when in fact it never ran."""
    assert ballot.band_outcomes(
        {"cutoff_band": None, "balloted": False, "vacancies": 0, "applicants": 0}
    ) is None


# --- fetch resilience ------------------------------------------------------

class _FakeResponse:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass


class _FakeSession:
    """Serves a payload-less shell first, then the real page — the CDN-variant
    case that CI hit while the same URL served 728 KB elsewhere."""
    def __init__(self, bodies): self.bodies, self.urls = list(bodies), []
    def get(self, url, **kw):
        self.urls.append(url)
        return _FakeResponse(self.bodies.pop(0) if self.bodies else "")


def test_fetch_retries_when_the_page_comes_back_without_the_payload(monkeypatch):
    monkeypatch.setattr(ballot.time, "sleep", lambda *_: None)
    shell = "<html><body>no payload</body></html>"
    session = _FakeSession([shell, shell, html()])
    got = ballot.fetch(session=session)
    assert '"schoolData"' in ballot._flight_payload(got)
    assert len(session.urls) == 3, "should have retried past both empty bodies"


def test_fetch_varies_the_cache_key_between_attempts(monkeypatch):
    """Retrying the identical URL would just be served the same poisoned edge
    object, so each attempt has to look like a different request."""
    monkeypatch.setattr(ballot.time, "sleep", lambda *_: None)
    session = _FakeSession(["<html></html>", html()])
    ballot.fetch(session=session)
    assert len(set(session.urls)) == len(session.urls)


def test_fetch_gives_up_rather_than_looping(monkeypatch):
    """A 200 with no payload is indistinguishable from a layout change, so it
    must eventually raise — the caller keeps the archived years either way."""
    monkeypatch.setattr(ballot.time, "sleep", lambda *_: None)
    session = _FakeSession(["<html></html>"] * 10)
    with pytest.raises(ballot.BallotError, match="no schoolData"):
        ballot.fetch(session=session)
    assert len(session.urls) == ballot.FETCH_ATTEMPTS
