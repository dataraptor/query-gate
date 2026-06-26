"""QueryGate synthetic EHR/claims seed loader (spec §8, §11, Appendix C).

Loads a *byte-identical* synthetic database on every run: `Faker(seed=42)` +
`random.seed(42)`, fixed row counts, and **time-stable date bands** so time-relative
questions ("how many overdue") give the same answer today or years from now.

Connects as the ADMIN role (DATABASE_URL). It applies scripts/schema.sql (idempotent),
truncates the app tables, inserts the fixed dataset, and runs ANALYZE so
`pg_class.reltuples` is populated. It never touches the read-only `querygate` role.

Usage:
    python -m scripts.seed              # ensure schema + (re)load data
    python scripts/seed.py --reset      # same, explicit truncate-and-reload

`--reset` truncates with RESTART IDENTITY (it does NOT drop the schema) so the
Layer-3 grants on the tables survive a reseed. Re-running produces identical data.
"""

from __future__ import annotations

import argparse
import os
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import psycopg
from faker import Faker

# ---------------------------------------------------------------------------
# Determinism knobs — change nothing here without re-pinning the expected counts
# in tests/test_seed_and_role.py. The seeds + pinned Faker (pyproject.toml) make
# the dataset byte-identical across machines and across time.
# ---------------------------------------------------------------------------
SEED = 42

# Fixed row counts (assertable in tests — NOT random ranges).
N_PATIENTS = 500
N_PROVIDERS = 50
ENCOUNTERS_PER_PATIENT = 8          # 500 * 8 = 4000 encounters (and 4000 claims, 1:1)
N_ENCOUNTERS = N_PATIENTS * ENCOUNTERS_PER_PATIENT
N_CLAIMS = N_ENCOUNTERS

# follow_ups cover a known fraction of patients, split into two time-stable bands.
N_OVERDUE = 300                     # durably-overdue: due_date years past, completed_at NULL
N_NOT_DUE = 180                     # durably-not-due: due_date years in the future
N_FOLLOW_UPS = N_OVERDUE + N_NOT_DUE  # 480 distinct patients (patient_id 1..480)

# The money-demo quirk: provider #1 is given clearly the most overdue patients.
TOP_PROVIDER_OVERDUE = 60           # patients 1..60 (overdue) -> provider 1; rest spread 2..50

# The search_text quirk: exactly one "Sarah Lee", and no "Sara Lee" (forces fuzzy match).
SARAH_LEE_PATIENT_INDEX = 100       # 0-based index into the patient list

# Time-stable date bands (Appendix C / §11). Absolute, fixed dates — NOT relative to
# now() — so the seed is byte-identical forever AND the overdue/not-due classification
# stays correct for any "now" in roughly 2024..2029 (a ~7-year gap with nothing inside).
DOB_START, DOB_END = date(1940, 1, 1), date(2006, 1, 1)
REGISTERED_START, REGISTERED_END = date(2014, 1, 1), date(2021, 12, 31)
CONTACTED_START, CONTACTED_END = date(2019, 1, 1), date(2022, 12, 31)
ENCOUNTER_START, ENCOUNTER_END = date(2018, 1, 1), date(2022, 12, 31)
OVERDUE_START, OVERDUE_END = date(2016, 1, 1), date(2022, 12, 31)   # < now()  (overdue)
NOT_DUE_START, NOT_DUE_END = date(2031, 1, 1), date(2036, 12, 31)   # > now()  (not due)

SPECIALTIES = [
    "Cardiology", "Cardiology", "Pediatrics", "Oncology", "Dermatology",
    "Neurology", "Orthopedics", "Psychiatry", "Radiology", "General Practice",
]
SEX = ["M", "F"]
ENCOUNTER_TYPES = ["office_visit", "telehealth", "lab", "imaging", "follow_up"]
ENCOUNTER_STATUS = ["completed", "completed", "completed", "no_show", "cancelled"]
CLAIM_STATUS = ["paid", "paid", "submitted", "denied"]

SCHEMA_SQL = Path(__file__).with_name("schema.sql")


def _rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def _rand_ts(rng: random.Random, start: date, end: date) -> datetime:
    """A timezone-aware (UTC) timestamp uniformly within [start, end]."""
    day = _rand_date(rng, start, end)
    secs = rng.randint(0, 86399)
    return datetime.combine(day, time(0, 0), tzinfo=timezone.utc) + timedelta(seconds=secs)


def _build_dataset():
    """Return the full dataset as lists of tuples. Pure + deterministic given SEED."""
    fake = Faker()
    Faker.seed(SEED)
    rng = random.Random(SEED)

    # --- providers -------------------------------------------------------
    providers = []
    for pid in range(1, N_PROVIDERS + 1):
        providers.append((
            pid,
            fake.name(),
            SPECIALTIES[(pid - 1) % len(SPECIALTIES)],
            "".join(str(rng.randint(0, 9)) for _ in range(10)),  # 10-digit NPI
        ))

    # --- primary-provider assignment (drives the money-demo top provider) ---
    # Overdue patients are patient_id 1..N_OVERDUE. The first TOP_PROVIDER_OVERDUE of
    # them go to provider 1; the remaining overdue patients spread across 2..50 (<=5
    # each), so provider 1 is the strict, unique top of "overdue patients per provider".
    primary_provider: dict[int, int] = {}
    for i in range(N_PATIENTS):
        patient_id = i + 1
        if patient_id <= N_OVERDUE:
            if patient_id <= TOP_PROVIDER_OVERDUE:
                primary_provider[patient_id] = 1
            else:
                primary_provider[patient_id] = 2 + ((patient_id - TOP_PROVIDER_OVERDUE - 1) % (N_PROVIDERS - 1))
        else:
            # not-due / no-follow-up patients: round-robin over all providers (irrelevant
            # to the overdue group-by, kept simple and deterministic).
            primary_provider[patient_id] = 1 + (patient_id % N_PROVIDERS)

    # --- patients --------------------------------------------------------
    patients = []
    for i in range(N_PATIENTS):
        patient_id = i + 1
        name = "Sarah Lee" if i == SARAH_LEE_PATIENT_INDEX else fake.name()
        registered = _rand_ts(rng, REGISTERED_START, REGISTERED_END)
        # ~half the patients have a last_contacted_at; the rest are NULL.
        last_contacted = _rand_ts(rng, CONTACTED_START, CONTACTED_END) if rng.random() < 0.5 else None
        patients.append((
            patient_id,
            name,
            _rand_date(rng, DOB_START, DOB_END),
            rng.choice(SEX),
            fake.city(),
            registered,
            last_contacted,
        ))

    # --- encounters + claims (1 claim per encounter) ---------------------
    encounters = []
    claims = []
    encounter_id = 0
    claim_id = 0
    for i in range(N_PATIENTS):
        patient_id = i + 1
        provider_id = primary_provider[patient_id]
        for _ in range(ENCOUNTERS_PER_PATIENT):
            encounter_id += 1
            enc_date = _rand_date(rng, ENCOUNTER_START, ENCOUNTER_END)
            encounters.append((
                encounter_id,
                patient_id,
                provider_id,
                enc_date,
                rng.choice(ENCOUNTER_TYPES),
                rng.choice(ENCOUNTER_STATUS),
            ))
            # one claim per encounter
            claim_id += 1
            status = rng.choice(CLAIM_STATUS)
            submitted = datetime.combine(enc_date, time(0, 0), tzinfo=timezone.utc) + timedelta(
                days=rng.randint(0, 5), seconds=rng.randint(0, 86399)
            )
            paid = (submitted + timedelta(days=rng.randint(3, 30))) if status == "paid" else None
            amount = round(rng.uniform(50.0, 5000.0), 2)
            claims.append((claim_id, encounter_id, amount, status, submitted, paid))

    # --- follow_ups (two time-stable bands) ------------------------------
    # patient_id 1..N_OVERDUE  -> overdue band (due_date past, completed_at NULL)
    # patient_id N_OVERDUE+1..N_FOLLOW_UPS -> not-due band (due_date future)
    follow_ups = []
    follow_up_id = 0
    for patient_id in range(1, N_OVERDUE + 1):
        follow_up_id += 1
        follow_ups.append((
            follow_up_id, patient_id, _rand_date(rng, OVERDUE_START, OVERDUE_END), None,
        ))
    for patient_id in range(N_OVERDUE + 1, N_FOLLOW_UPS + 1):
        follow_up_id += 1
        follow_ups.append((
            follow_up_id, patient_id, _rand_date(rng, NOT_DUE_START, NOT_DUE_END), None,
        ))

    return providers, patients, encounters, claims, follow_ups


def _exec_script(conn: psycopg.Connection, sql_path: Path) -> None:
    """Run a .sql file via psycopg.

    psycopg3 executes a multi-statement script (no parameters) in a single call, with
    the server parsing comments and statement boundaries — so this is robust to
    semicolons inside comments. schema.sql contains no dollar-quoted bodies.
    """
    conn.execute(sql_path.read_text(encoding="utf-8"))


def seed(conn: psycopg.Connection, *, reset: bool = True) -> dict[str, int]:
    """Apply the schema (idempotent), (re)load the fixed dataset, and ANALYZE.

    Returns the per-table row counts that were inserted.
    """
    providers, patients, encounters, claims, follow_ups = _build_dataset()

    _exec_script(conn, SCHEMA_SQL)

    # Truncate in FK-safe order via CASCADE + RESTART IDENTITY. This preserves the
    # Layer-3 grants on the tables (DROP would not), so a reseed never loosens the role.
    conn.execute(
        "TRUNCATE app.follow_ups, app.claims, app.encounters, app.providers, app.patients "
        "RESTART IDENTITY CASCADE"
    )

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO app.providers (provider_id, name, specialty, npi) VALUES (%s, %s, %s, %s)",
            providers,
        )
        cur.executemany(
            "INSERT INTO app.patients "
            "(patient_id, name, dob, sex, city, registered_at, last_contacted_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            patients,
        )
        cur.executemany(
            "INSERT INTO app.encounters "
            "(encounter_id, patient_id, provider_id, date, type, status) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            encounters,
        )
        cur.executemany(
            "INSERT INTO app.claims "
            "(claim_id, encounter_id, amount, status, submitted_at, paid_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            claims,
        )
        cur.executemany(
            "INSERT INTO app.follow_ups (follow_up_id, patient_id, due_date, completed_at) "
            "VALUES (%s, %s, %s, %s)",
            follow_ups,
        )
    conn.commit()

    # ANALYZE so pg_class.reltuples is populated (else list_tables reads -1 later).
    # ANALYZE cannot run inside a transaction block; use autocommit for it.
    prev_autocommit = conn.autocommit
    conn.autocommit = True
    try:
        conn.execute("ANALYZE app.patients, app.providers, app.encounters, app.claims, app.follow_ups")
    finally:
        conn.autocommit = prev_autocommit

    return {
        "patients": len(patients),
        "providers": len(providers),
        "encounters": len(encounters),
        "claims": len(claims),
        "follow_ups": len(follow_ups),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the QueryGate synthetic database.")
    parser.add_argument(
        "--reset", action="store_true",
        help="Truncate and reload (default behaviour; flag kept for explicitness / CLI parity).",
    )
    parser.add_argument(
        "--database-url", default=os.environ.get("DATABASE_URL"),
        help="Admin connection string (defaults to $DATABASE_URL).",
    )
    args = parser.parse_args()
    if not args.database_url:
        parser.error("no admin connection string: set $DATABASE_URL or pass --database-url")

    with psycopg.connect(args.database_url) as conn:
        counts = seed(conn, reset=True)
    total = sum(counts.values())
    print(f"Seeded {total} rows: " + ", ".join(f"{k}={v}" for k, v in counts.items()))


if __name__ == "__main__":
    main()
