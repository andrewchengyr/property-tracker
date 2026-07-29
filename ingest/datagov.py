"""Shared data.gov.sg access.

Both the HDB resale client and the school directory hit the same host and the
same rate limiter, so the retry policy lives here rather than in one of them.
It was in `hdb.py` alone, and the schools pull — which runs straight after four
HDB calls — took a 429 and failed the whole scheduled run.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

SEARCH_URL = "https://data.gov.sg/api/action/datastore_search"

TIMEOUT = 60
MAX_RETRIES = 5
BACKOFF_BASE = 3  # seconds: 3, 6, 12, 24


class DataGovError(RuntimeError):
    pass


def get(
    params: dict[str, Any], session: requests.Session | None = None
) -> requests.Response:
    """GET datastore_search, retrying 429/5xx with exponential backoff and
    honouring Retry-After when the server sends one."""
    session = session or requests.Session()
    last: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            r = session.get(SEARCH_URL, params=params, timeout=TIMEOUT)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After") or BACKOFF_BASE * 2**attempt)
                log.warning(
                    "data.gov.sg returned %d, retrying in %.0fs (attempt %d/%d)",
                    r.status_code, wait, attempt + 1, MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as exc:
            last = exc
            if attempt == MAX_RETRIES - 1:
                break
            wait = BACKOFF_BASE * 2**attempt
            log.warning("data.gov.sg request failed (%s), retrying in %ds", exc, wait)
            time.sleep(wait)

    raise DataGovError(f"data.gov.sg unreachable after {MAX_RETRIES} attempts: {last}")
