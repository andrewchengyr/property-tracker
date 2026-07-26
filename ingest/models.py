"""Normalized transaction schema shared by both sources.

URA caveats and HDB resale records have entirely different field names and
shapes. Everything upstream converts into `Transaction` so the store, the
exporter and the frontend only ever deal with one thing.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

SQFT_PER_SQM = 10.7639

SOURCE_URA = "URA"
SOURCE_HDB = "HDB"


@dataclass
class Transaction:
    source: str  # "URA" | "HDB"
    property_name: str  # URA project name, or HDB "BLOCK STREET"
    property_type: str  # Condominium / Apartment / EC / HDB flat_type
    segment: str  # URA market segment (CCR/RCR/OCR) or HDB town
    address: str
    district_town: str
    txn_date: date  # normalized to the first of the month
    price: float
    area_sqm: float
    storey_range: str
    tenure: str
    lat: float | None = None
    lng: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def area_sqft(self) -> float:
        return self.area_sqm * SQFT_PER_SQM

    @property
    def price_psf(self) -> float | None:
        sqft = self.area_sqft
        return self.price / sqft if sqft else None

    def dedup_key(self) -> tuple:
        """The natural key that makes re-ingestion idempotent.

        Coordinates are deliberately NOT part of this. The spec keyed URA rows
        on lat/lng because caveats were documented as carrying their own SVY21
        coordinates — but the live feed returns none, so they are geocoded
        afterwards. A key containing a *derived, later-filled* field silently
        breaks idempotency: the same caveat hashes one way before geocoding and
        another way after, and the second run duplicates every row instead of
        updating it. Identity is the transaction itself.
        """
        return (
            self.source,
            self.property_name,
            self.txn_date.isoformat(),
            round(float(self.price), 2),
            round(float(self.area_sqm), 2),
            self.storey_range,
        )

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        raw = d.pop("raw")
        d["txn_date"] = self.txn_date.isoformat()
        d["area_sqft"] = round(self.area_sqft, 2)
        psf = self.price_psf
        d["price_psf"] = round(psf, 2) if psf is not None else None
        d["raw_json"] = json.dumps(raw, sort_keys=True)
        return d


def parse_ura_contract_date(mmyy: str) -> date:
    """URA `contractDate` is MMYY — "0324" is March 2024, "0125" is Jan 2025.

    Deliberately strict: a malformed value should surface as a skipped record
    with a warning, never as a silently wrong date.
    """
    s = str(mmyy).strip()
    if len(s) != 4 or not s.isdigit():
        raise ValueError(f"expected MMYY, got {mmyy!r}")
    month, year = int(s[:2]), int(s[2:])
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range in {mmyy!r}")
    return date(2000 + year, month, 1)


# "99 yrs lease commencing from 2008", "999 yrs lease commencing from 1875"
_TENURE_RE = re.compile(r"(\d{2,4})\s*yrs?\s*lease\s*commencing\s*from\s*(\d{4})", re.I)


def lease_facts(source: str, tenure: str, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """Normalize tenure into label + start year + term, for the "years left"
    countdown the frontend computes against today.

    HDB caveats carry `lease_commence_date` and a *remaining* lease that is
    only true as of that transaction; the start year is the stable fact, so
    that is what gets exported.
    """
    raw = raw or {}
    tenure = (tenure or "").strip()

    if source == SOURCE_HDB:
        start = _int_or_none(raw.get("lease_commence_date"))
        return {
            "tenure_label": "99-year leasehold",
            "lease_start": start,
            "lease_years": 99,
            "flat_model": str(raw.get("flat_model") or "").strip(),
        }

    if "freehold" in tenure.lower():
        return {"tenure_label": "Freehold", "lease_start": None, "lease_years": None}

    m = _TENURE_RE.search(tenure)
    if m:
        years, start = int(m.group(1)), int(m.group(2))
        return {
            "tenure_label": f"{years}-year leasehold",
            "lease_start": start,
            "lease_years": years,
        }

    # Unrecognised: surface it verbatim rather than guessing at a term.
    return {"tenure_label": tenure or "", "lease_start": None, "lease_years": None}


def _int_or_none(v: Any) -> int | None:
    try:
        return int(str(v).strip()[:4])
    except (TypeError, ValueError):
        return None


def parse_hdb_month(month: str) -> date:
    """HDB `month` is YYYY-MM."""
    s = str(month).strip()
    parts = s.split("-")
    if len(parts) != 2:
        raise ValueError(f"expected YYYY-MM, got {month!r}")
    return date(int(parts[0]), int(parts[1]), 1)
