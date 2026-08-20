"""P1 registration vacancies and balloting, from MOE.

MOE publishes one exercise at a time. The page for the 2025 exercise replaces
the page for 2024, and the archived copies of the older pages are shells — the
data used to be fetched client-side and no crawler kept it. So the only way to
have three years of this is to start keeping them: every year's run appends,
nothing is ever deleted, exactly as the committed transaction archive exists
because URA serves a rolling five-year window (PLAN §5.1).

The data is embedded in the page as a Next.js flight payload rather than served
by an API, so it is parsed out of the HTML. That is brittle by nature — a site
rebuild will break it — which is why a failure here degrades to "no balloting
data" and never takes the run down with it.

The interesting field is `balloting_content_copy`, one sentence naming the
distance band the school filled at. That band is a **cut-off, not a flag**:
"balloting conducted for children within 1km" means the school ran out inside
1 km, so applicants in 1-2 km and beyond got nothing at all. Read as three
independent yes/no flags it says close to the opposite of what it means, which
is why `band_outcomes` exists.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

URL = ("https://www.moe.gov.sg/primary/p1-registration/"
       "past-vacancies-and-balloting-data")
USER_AGENT = "Mozilla/5.0 (compatible; sg-property-tracker/1.0)"
TIMEOUT = 90

# The three bands MOE registers against, nearest first. Order matters: the
# outcome of every band is decided by where it sits relative to the cut-off.
BANDS = ("within_1km", "1_2km", "outside_2km")
BAND_LABELS = {"within_1km": "Within 1 km", "1_2km": "1–2 km",
               "outside_2km": "Beyond 2 km"}

# Phases that can ballot. 1 and 2A(1)/2A(2) legacy codes aside, these are what
# the current payload carries; 0 and 1 are sibling/alumni phases with no
# distance rule, kept for the vacancy counts but never balloted.
BALLOTING_PHASES = ("2A", "2B", "2C", "2CS")

# MOE's own school naming differs from the school directory's in exactly one
# place; both lists were enumerated to confirm it. The directory omits the
# "(Primary)" suffix that the balloting page puts on through-train schools,
# which the name normaliser already strips, so only this one needs an alias.
NAME_ALIASES = {"ST ANDREW S JUNIOR SCHOOL": "ST ANDREW S SCHOOL JUNIOR"}


class BallotError(RuntimeError):
    pass


def normalise_name(name: Any) -> str:
    """Join key between MOE's balloting page and the school directory.

    Punctuation differs freely between the two (`CHIJ St. Nicholas Girls'`
    vs `CHIJ ST. NICHOLAS GIRLS'`), and the balloting page appends
    `(Primary)` to through-train schools, so everything non-alphanumeric is
    collapsed and the suffix dropped.
    """
    n = str(name or "").upper().replace("&", "AND")
    n = re.sub(r"[^A-Z0-9]+", " ", n).strip()
    n = re.sub(r"\s+PRIMARY$", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return NAME_ALIASES.get(n, n)


# ---------------------------------------------------------------- fetch -----

FETCH_ATTEMPTS = 4
FETCH_BACKOFF = 4


def fetch(session: requests.Session | None = None, url: str = URL) -> str:
    """Fetch the page, insisting that the payload is actually in it.

    The site sits behind CloudFront and varies on `rsc` and the Next.js router
    headers, so an edge can hold a variant of this URL that renders the shell
    without `schoolData`. A 200 with no payload is what CI got while the same
    request from a different network returned the full 728 KB — so a bare
    status check is not enough to know the fetch worked.

    Each attempt asks the CDN to revalidate and varies the cache key, then
    retries on a body that came back without the payload. It still gives up
    rather than looping: the caller keeps the archived years either way.
    """
    session = session or requests.Session()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    last = ""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        # Vary the query string so a poisoned edge object is not reused. The
        # page ignores unknown parameters.
        r = session.get(f"{url}?cb={attempt}", headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        last = r.text
        if '"schoolData"' in _flight_payload(last):
            if attempt > 1:
                log.info("MOE page carried the payload on attempt %d", attempt)
            return last
        # Log what actually came back. The first version of this guessed at a
        # CDN cache variant and was wrong: the body is ~2.4 KB, far too small
        # to be even a shell of this page, so it is a different response
        # entirely. Never diagnose this from the size alone again.
        log.warning(
            "MOE page returned %d chars with no schoolData (attempt %d/%d); "
            "status %s, content-type %s, body starts: %s",
            len(last), attempt, FETCH_ATTEMPTS, r.status_code,
            r.headers.get("content-type", "?"),
            " ".join(last[:400].split())[:300],
        )
        if attempt < FETCH_ATTEMPTS:
            time.sleep(FETCH_BACKOFF * attempt)

    raise BallotError(
        f"MOE page had no schoolData after {FETCH_ATTEMPTS} attempts "
        f"(last body {len(last)} chars) — CDN variant, or the layout changed"
    )


def _flight_payload(html: str) -> str:
    """Reassemble the streamed RSC chunks into one string, the way the client
    would. Each chunk is a JS string literal, so json.loads unescapes it."""
    out = []
    for m in re.finditer(r'__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)', html):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return "".join(out)


def _extract_array(payload: str, key: str = '"schoolData":') -> list[dict[str, Any]]:
    """Slice out one JSON array by counting brackets, skipping over strings so
    that a bracket inside a remark can't end the array early."""
    i = payload.find(key)
    if i < 0:
        raise BallotError(f"{key} not found — the MOE page layout has changed")
    start = payload.index("[", i)
    depth, j = 0, start
    while j < len(payload):
        ch = payload[j]
        if ch == '"':                       # skip the whole string literal
            j += 1
            while j < len(payload) and payload[j] != '"':
                j += 2 if payload[j] == "\\" else 1
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return json.loads(payload[start:j + 1])
        j += 1
    raise BallotError("unterminated schoolData array")


def parse(html: str) -> list[dict[str, Any]]:
    """One flat row per school per phase per year."""
    data = _extract_array(_flight_payload(html))
    rows: list[dict[str, Any]] = []
    for school in data:
        for item in school.get("phase_items") or []:
            it = item.get("school_phase_item_id") or {}
            name = it.get("school_name")
            year, phase = it.get("year"), it.get("phase")
            if not (name and year and phase):
                continue
            copy = _clean(it.get("balloting_content_copy"))
            band, who = classify(copy)
            rows.append({
                "school_key": normalise_name(name),
                "school_name": str(name).strip(),
                "year": str(year).strip(),
                "phase": str(phase).strip().upper(),
                "vacancies": _int(it.get("total_vacancies")),
                "applicants": _int(it.get("total_applicants")),
                "balloted": bool(it.get("balloting_required")),
                "vacancies_balloted": _int(it.get("vacancies_balloted")),
                "applicants_balloted": _int(it.get("applicants_balloted")),
                "cutoff_band": band,
                "cohort": who,
                "note": copy,
            })
    log.info("MOE balloting: %d rows across %d schools",
             len(rows), len({r["school_key"] for r in rows}))
    return rows


def _int(v: Any) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _clean(v: Any) -> str:
    """Most notes are plain sentences; one carries HTML."""
    text = re.sub(r"<[^>]+>", " ", str(v or ""))
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- parse -----

_WITHIN_1 = re.compile(r"within\s*1\s*km", re.I)
_BETWEEN = re.compile(r"between\s*1\s*km\s*and\s*2\s*km", re.I)
_OUTSIDE_2 = re.compile(r"outside\s*2\s*km", re.I)
_WITHIN_2 = re.compile(r"within\s*2\s*km", re.I)
_ALL_SC = re.compile(r"offered\s+to\s+all\s+singapore\s+citizen", re.I)
_PR = re.compile(r"permanent\s+resident", re.I)
_NO_BALLOT = re.compile(r"no\s+balloting", re.I)


def classify(note: str) -> tuple[str | None, str | None]:
    """(cut-off band, cohort) from MOE's sentence.

    Returns (None, None) for anything unrecognised rather than guessing — an
    unseen wording shows the sentence verbatim and no derived verdict.
    """
    if not note:
        return None, None
    cohort = "PR" if _PR.search(note) and not _ALL_SC.search(note) else "SC"

    if _ALL_SC.search(note):
        return "all", cohort
    if _BETWEEN.search(note):
        return "1_2km", cohort
    if _OUTSIDE_2.search(note):
        return "outside_2km", cohort
    if _WITHIN_2.search(note):
        return "within_2km", cohort
    if _WITHIN_1.search(note):
        return "within_1km", cohort

    log.warning("unrecognised balloting note, no verdict derived: %r", note[:160])
    return None, None


def band_outcomes(row: dict[str, Any]) -> dict[str, str] | None:
    """What each distance band actually got, nearest band first.

        "in"      admitted without balloting
        "ballot"  balloting decided it
        "none"    the school filled before reaching this band

    This is the whole point of the feature. The cut-off band is where the
    school ran out: everything nearer got in outright, everything further away
    got nothing. A school that balloted "within 1km" is the *hardest* case for
    a 1-2 km buyer, not a reassuring one.
    """
    band, balloted = row.get("cutoff_band"), row.get("balloted")

    if not band:
        # MOE leaves the sentence empty when a phase was never contested, which
        # is most of them. Undersubscribed and unballoted means every applicant
        # was placed regardless of where they lived — derivable, and far more
        # useful than a blank row. Anything else stays unknown.
        vac, app = row.get("vacancies"), row.get("applicants")
        # Zero vacancies means the phase was never run for this school, which
        # is not the same as "everyone got in" — reporting it as an easy phase
        # would say the opposite of the truth.
        if not balloted and vac and app is not None and app <= vac:
            return {b: "in" for b in BANDS}
        return None
    if band == "all":
        return {b: "in" for b in BANDS}
    if band == "within_2km":                       # no ballot, filled at 2 km
        return {"within_1km": "in", "1_2km": "in", "outside_2km": "none"}
    if band not in BANDS:
        return None

    cut = BANDS.index(band)
    return {
        b: ("in" if i < cut else ("ballot" if i == cut and balloted else
                                  "in" if i == cut else "none"))
        for i, b in enumerate(BANDS)
    }


def rows_by_school(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["school_key"], []).append(r)
    for v in out.values():
        v.sort(key=lambda r: (r["year"], r["phase"]))
    return out
