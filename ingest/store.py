"""SQLite store — canonical data, idempotent upserts, and the exports.

The spec defines a different composite uniqueness key per source. Rather than
two partial indexes, each row carries a computed `dedup_key` string (built by
`Transaction.dedup_key`) under one UNIQUE index. Same guarantee, one code path,
and the key travels with the model that defines it.

URA revises and sometimes voids past caveats, so a conflict updates the row
in place instead of being ignored — the store tracks the freshest view.
"""

from __future__ import annotations

import csv
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import Transaction, lease_facts

log = logging.getLogger(__name__)

DB_PATH = Path("data/transactions.db")
EXPORT_JSON = Path("web/data.json")
EXPORT_DIR = Path("data/exports")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,
    dedup_key     TEXT    NOT NULL UNIQUE,
    source        TEXT    NOT NULL,
    property_name TEXT    NOT NULL,
    property_type TEXT,
    segment       TEXT,
    address       TEXT,
    district_town TEXT,
    txn_date      TEXT    NOT NULL,
    price         REAL    NOT NULL,
    area_sqm      REAL,
    area_sqft     REAL,
    price_psf     REAL,
    storey_range  TEXT,
    tenure        TEXT,
    flat_model    TEXT,
    lat           REAL,
    lng           REAL,
    raw_json      TEXT,
    first_seen    TEXT,
    last_seen     TEXT
);

CREATE INDEX IF NOT EXISTS idx_txn_property ON transactions (source, property_name);
CREATE INDEX IF NOT EXISTS idx_txn_date     ON transactions (txn_date);

-- Rent per sqft per month, per property per month. Its own table rather than
-- columns on transactions: a rental and a sale are different events, and a
-- property can have one without the other.
CREATE TABLE IF NOT EXISTS rentals (
    source        TEXT NOT NULL,
    property_name TEXT NOT NULL,
    property_type TEXT NOT NULL,
    period        TEXT NOT NULL,
    rent_psf      REAL NOT NULL,
    contracts     INTEGER,
    PRIMARY KEY (source, property_name, property_type, period)
);

-- P1 registration outcomes, one row per school/year/phase. Accumulating by
-- design: MOE publishes only the current exercise and replaces it each year,
-- so like the transaction archive this is the only copy of anything older.
-- Nothing here is ever deleted.
CREATE TABLE IF NOT EXISTS p1_ballot (
    school_key         TEXT NOT NULL,
    school_name        TEXT NOT NULL,
    year               TEXT NOT NULL,
    phase              TEXT NOT NULL,
    vacancies          INTEGER,
    applicants         INTEGER,
    balloted           INTEGER,
    vacancies_balloted INTEGER,
    applicants_balloted INTEGER,
    cutoff_band        TEXT,
    cohort             TEXT,
    note               TEXT,
    PRIMARY KEY (school_key, year, phase)
);

CREATE TABLE IF NOT EXISTS geo (
    address     TEXT PRIMARY KEY,
    lat         REAL NOT NULL,
    lng         REAL NOT NULL,
    geocoded_at TEXT
);
"""

UPSERT = """
INSERT INTO transactions (
    dedup_key, source, property_name, property_type, segment, address,
    district_town, txn_date, price, area_sqm, area_sqft, price_psf,
    storey_range, tenure, flat_model, lat, lng, raw_json, first_seen, last_seen
) VALUES (
    :dedup_key, :source, :property_name, :property_type, :segment, :address,
    :district_town, :txn_date, :price, :area_sqm, :area_sqft, :price_psf,
    :storey_range, :tenure, :flat_model, :lat, :lng, :raw_json, :now, :now
)
ON CONFLICT(dedup_key) DO UPDATE SET
    property_type = excluded.property_type,
    segment       = excluded.segment,
    address       = excluded.address,
    district_town = excluded.district_town,
    area_sqft     = excluded.area_sqft,
    price_psf     = excluded.price_psf,
    tenure        = excluded.tenure,
    flat_model    = excluded.flat_model,
    -- never overwrite a known coordinate with a null
    lat           = COALESCE(excluded.lat, transactions.lat),
    lng           = COALESCE(excluded.lng, transactions.lng),
    raw_json      = excluded.raw_json,
    last_seen     = excluded.last_seen
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower())
    return s.strip("-") or "property"


class Store:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Additive column migrations for a database committed by an earlier
        version. CREATE TABLE IF NOT EXISTS won't add columns to a table that
        already exists, and the db is version-controlled, so an older one will
        be checked out by CI and by anyone cloning the repo."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(transactions)")}
        for column, ddl in (("flat_model", "TEXT"),):
            if column not in have:
                log.info("migrating: adding transactions.%s", column)
                self.conn.execute(f"ALTER TABLE transactions ADD COLUMN {column} {ddl}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- geo cache -------------------------------------------------------

    def get_geo(self, address: str) -> tuple[float, float] | None:
        row = self.conn.execute(
            "SELECT lat, lng FROM geo WHERE address = ?", (address,)
        ).fetchone()
        return (row["lat"], row["lng"]) if row else None

    def put_geo(self, address: str, lat: float, lng: float) -> None:
        self.conn.execute(
            "INSERT INTO geo (address, lat, lng, geocoded_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(address) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, "
            "geocoded_at=excluded.geocoded_at",
            (address, lat, lng, _now()),
        )
        self.conn.commit()

    # ---- transactions ----------------------------------------------------

    def upsert_many(self, txns: Sequence[Transaction]) -> tuple[int, int]:
        """Returns (added, updated). Re-running with the same data yields
        (0, n) — that idempotency is the point."""
        if not txns:
            return 0, 0

        keys = [json.dumps(t.dedup_key()) for t in txns]
        existing = self._existing_keys(keys)

        now = _now()
        added = updated = 0
        for txn, key in zip(txns, keys):
            row = txn.to_row()
            row["dedup_key"] = key
            row["now"] = now
            self.conn.execute(UPSERT, row)
            if key in existing:
                updated += 1
            else:
                added += 1
                existing.add(key)  # guards against duplicates inside one batch
        self.conn.commit()
        return added, updated

    def _existing_keys(self, keys: Iterable[str]) -> set[str]:
        found: set[str] = set()
        keys = list(keys)
        for i in range(0, len(keys), 500):  # stay under SQLite's variable limit
            chunk = keys[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            rows = self.conn.execute(
                f"SELECT dedup_key FROM transactions WHERE dedup_key IN ({placeholders})",
                chunk,
            ).fetchall()
            found.update(r["dedup_key"] for r in rows)
        return found

    def upsert_rentals(self, rows: Sequence[tuple[str, str, str, str, float, int]]) -> int:
        """(source, name, type, period, rent_psf, contracts). Idempotent on the
        natural key, like transactions."""
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO rentals (source, property_name, property_type, period, "
            "rent_psf, contracts) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source, property_name, property_type, period) DO UPDATE SET "
            "rent_psf = excluded.rent_psf, contracts = excluded.contracts",
            rows,
        )
        self.conn.commit()
        return len(rows)

    def upsert_ballot(self, rows: Sequence[dict[str, Any]]) -> int:
        """Idempotent on (school, year, phase). MOE corrects this data after
        publication, so a conflict updates in place rather than being ignored —
        same reasoning as the URA caveat revisions."""
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO p1_ballot (school_key, school_name, year, phase, "
            "vacancies, applicants, balloted, vacancies_balloted, "
            "applicants_balloted, cutoff_band, cohort, note) "
            "VALUES (:school_key, :school_name, :year, :phase, :vacancies, "
            ":applicants, :balloted, :vacancies_balloted, :applicants_balloted, "
            ":cutoff_band, :cohort, :note) "
            "ON CONFLICT(school_key, year, phase) DO UPDATE SET "
            "vacancies=excluded.vacancies, applicants=excluded.applicants, "
            "balloted=excluded.balloted, "
            "vacancies_balloted=excluded.vacancies_balloted, "
            "applicants_balloted=excluded.applicants_balloted, "
            "cutoff_band=excluded.cutoff_band, cohort=excluded.cohort, "
            "note=excluded.note",
            [{**r, "balloted": int(bool(r.get("balloted")))} for r in rows],
        )
        self.conn.commit()
        return len(rows)

    def ballot_years(self) -> set:
        return {r["year"] for r in self.conn.execute(
            "SELECT DISTINCT year FROM p1_ballot")}

    def ballot_by_school(self) -> dict:
        """school_key -> rows, newest year first. Read from the database, not
        from the pull, so previously archived years survive a year in which the
        MOE page cannot be parsed."""
        out: dict = {}
        for r in self.conn.execute(
            "SELECT * FROM p1_ballot ORDER BY year DESC, phase ASC"
        ):
            out.setdefault(r["school_key"], []).append(dict(r))
        return out

    def median_areas_sqft(self) -> dict:
        """(property_name, property_type) -> median floor area, for turning
        HDB's per-unit rent into a per-sqft figure."""
        out: dict = {}
        rows = self.conn.execute(
            "SELECT property_name, property_type, area_sqft FROM transactions "
            "WHERE source = 'HDB' AND area_sqft IS NOT NULL"
        ).fetchall()
        buckets: dict = {}
        for r in rows:
            buckets.setdefault((r["property_name"], r["property_type"]), []).append(
                r["area_sqft"])
        for key, values in buckets.items():
            values.sort()
            out[key] = values[len(values) // 2]
        return out

    def backfill_coords(self, property_name: str, source: str, lat: float, lng: float) -> int:
        """Apply a late geocode to rows already stored without coordinates."""
        cur = self.conn.execute(
            "UPDATE transactions SET lat = ?, lng = ? "
            "WHERE property_name = ? AND source = ? AND (lat IS NULL OR lng IS NULL)",
            (lat, lng, property_name, source),
        )
        self.conn.commit()
        return cur.rowcount

    def all_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM transactions ORDER BY property_name, txn_date"
        ).fetchall()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]

    # ---- exports ---------------------------------------------------------

    def export_json(
        self,
        path: Path | str = EXPORT_JSON,
        top_years: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Pre-aggregated per property so the frontend needs no grouping logic.

        `top_years` maps a lowercased property name to its completion year —
        URA caveats don't carry TOP, so it comes from the watchlist when the
        user supplies it.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        top_years = {k.lower(): v for k, v in (top_years or {}).items()}

        # Keyed on flat type AND model, not just name. One HDB block can hold
        # more than one flat type (8 Joo Seng Rd has both 5 ROOM and
        # EXECUTIVE) and more than one model within a type (236 Lor 1 Toa
        # Payoh has executive maisonettes and apartments, ~142 vs ~166 sqm).
        # Grouping on name alone merged them into one marker with a blended
        # psf and whichever label sorted first.
        grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in self.all_rows():
            ptype = row["property_type"] or ""
            model = (row["flat_model"] or "") if row["source"] == "HDB" else ""
            key = (row["source"], row["property_name"], ptype, model)
            prop = grouped.get(key)
            if prop is None:
                slug = slugify(row["property_name"])
                if row["source"] == "HDB" and ptype:
                    slug = f"{slug}-{slugify(ptype)}"
                    if model:
                        slug = f"{slug}-{slugify(model)}"
                prop = grouped[key] = {
                    "id": f"{row['source'].lower()}-{slug}",
                    "name": row["property_name"],
                    "source": row["source"],
                    "type": row["property_type"] or "",
                    "segment": row["segment"] or "",
                    "address": row["address"] or "",
                    "district_town": row["district_town"] or "",
                    "lat": row["lat"],
                    "lng": row["lng"],
                    "txns": [],
                }
            # A project's rows can carry slightly different block coordinates;
            # the first non-null is a good enough marker position.
            if prop["lat"] is None and row["lat"] is not None:
                prop["lat"], prop["lng"] = row["lat"], row["lng"]

            prop["txns"].append(
                {
                    "date": row["txn_date"],
                    "price": row["price"],
                    "area_sqft": round(row["area_sqft"], 1) if row["area_sqft"] else None,
                    "psf": round(row["price_psf"], 1) if row["price_psf"] else None,
                    "storey": row["storey_range"] or "",
                }
            )

        # Lease facts come from the most recent row — tenure is a property of
        # the building, but a stale caveat can carry an outdated string.
        for (source, name, ptype, model), prop in grouped.items():
            row = self.conn.execute(
                "SELECT tenure, raw_json FROM transactions "
                "WHERE source = ? AND property_name = ? AND IFNULL(property_type,'') = ? "
                "AND (? = '' OR IFNULL(flat_model,'') = ?) "
                "ORDER BY txn_date DESC LIMIT 1",
                (source, name, ptype, model, model),
            ).fetchone()
            facts = {}
            if row:
                try:
                    facts = lease_facts(source, row["tenure"], json.loads(row["raw_json"] or "{}"))
                except Exception as exc:  # noqa: BLE001 — facts are decoration
                    log.warning("could not derive lease facts for %s: %s", name, exc)
            prop.update(facts)
            top = top_years.get(name.lower())
            # For HDB the lease commences on completion, so it doubles as TOP.
            if top is None and source == "HDB":
                top = facts.get("lease_start")
            prop["top_year"] = top

            # Rent per sqft per month, so the frontend can period-match it to
            # the sale psf and derive a gross yield for whatever range is shown.
            rents = self.conn.execute(
                "SELECT period, rent_psf, contracts FROM rentals "
                "WHERE source = ? AND property_name = ? AND property_type = ? "
                "ORDER BY period",
                (source, name, ptype),
            ).fetchall()
            prop["rents"] = [
                {"date": r["period"], "psf": round(r["rent_psf"], 3),
                 "n": r["contracts"]}
                for r in rents
            ]

            # One label spanning both sources, for the frontend's model filter:
            # HDB's flat model (DBSS / Improved / Maisonette / Apartment), or
            # the URA property type (Condominium / Terrace / …) which is the
            # equivalent level of description for private property.
            prop["model"] = (model or prop.get("type") or "").strip()

        properties = []
        for prop in grouped.values():
            prop["txns"].sort(key=lambda t: t["date"])
            latest_date = prop["txns"][-1]["date"] if prop["txns"] else None
            # Average across the latest month, so one outlier caveat doesn't
            # define the headline number.
            latest = [t["psf"] for t in prop["txns"] if t["date"] == latest_date and t["psf"]]
            prop["latest_psf"] = round(sum(latest) / len(latest), 1) if latest else None
            prop["txn_count"] = len(prop["txns"])
            properties.append(prop)

        properties.sort(key=lambda p: p["name"])
        payload = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "properties": properties,
        }
        path.write_text(json.dumps(payload, indent=2))
        log.info("wrote %s (%d properties)", path, len(properties))
        return payload

    def export_schools(self, schools: list[dict[str, Any]], path: Path | str) -> None:
        """Written only when there is something to write: a failed directory
        fetch must leave the last good file in place rather than blank the
        map's school layer."""
        path = Path(path)
        if not schools:
            log.warning("no schools to export — leaving %s as it is", path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)

        # Attach whatever P1 history the archive holds. Joined on the
        # normalised name because MOE's balloting page and the school
        # directory punctuate differently — see ballot.normalise_name.
        from .ballot import band_outcomes, normalise_name
        history = self.ballot_by_school()
        attached = 0
        for school in schools:
            rows = history.get(normalise_name(school["name"]))
            if not rows:
                continue
            attached += 1
            school["p1"] = [{
                "year": r["year"], "phase": r["phase"],
                "vacancies": r["vacancies"], "applicants": r["applicants"],
                "balloted": bool(r["balloted"]),
                "vacancies_balloted": r["vacancies_balloted"],
                "applicants_balloted": r["applicants_balloted"],
                "cohort": r["cohort"], "note": r["note"],
                "bands": band_outcomes(r),
            } for r in rows]
        log.info("P1 balloting attached to %d of %d schools", attached, len(schools))

        path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "schools": sorted(schools, key=lambda s: s["name"]),
        }, indent=2))
        log.info("wrote %s (%d primary schools)", path, len(schools))

    def export_masterplan(self, payload: dict[str, Any] | None, path: Path | str) -> None:
        """Same rule as the schools export: written only when there is
        something to write, so a failed download leaves the last good overlay
        in place rather than emptying the map's land-use layer.

        Written compactly rather than indented — this is 6k parcels of
        geometry that nobody reads by eye, and the indentation costs about a
        third of the file.
        """
        path = Path(path)
        if not payload or not payload.get("features"):
            log.warning("no master plan parcels to export — leaving %s as it is", path)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        body = dict(payload)
        body["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        path.write_text(json.dumps(body, separators=(",", ":")))
        log.info("wrote %s (%d parcels)", path, len(body["features"]))

    def export_csv(self, directory: Path | str = EXPORT_DIR) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / "transactions.csv"
        rows = self.all_rows()
        columns = [
            "source", "property_name", "property_type", "segment", "address",
            "district_town", "txn_date", "price", "area_sqm", "area_sqft",
            "price_psf", "storey_range", "tenure", "flat_model", "lat", "lng",
        ]
        with out.open("w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row[c] for c in columns})
        log.info("wrote %s (%d rows)", out, len(rows))
        return out
