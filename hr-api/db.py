"""
db.py — SQLite schema and seed data for the mock HR system.

The schema has two IDs on purpose:

  worker_id    immutable, never reused. The correlation anchor.
  employee_id  the business ID. Nullable, reusable, reassigned on
               contractor->FTE conversion. Deliberately unreliable.

Every hard case in the scenario matrix is a case where employee_id
lies and worker_id doesn't.

Run directly to create and seed the database:
    python hr-api/db.py
    python hr-api/db.py --reset
"""

import argparse
import datetime
import pathlib
import sqlite3

DB_PATH = pathlib.Path(__file__).parent / "hr.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    worker_id         TEXT PRIMARY KEY,
    employee_id       TEXT,
    first_name        TEXT NOT NULL,
    last_name         TEXT NOT NULL,
    preferred_name    TEXT,
    legal_name        TEXT,
    department        TEXT NOT NULL,
    cost_center       TEXT,
    job_title         TEXT,
    manager_worker_id TEXT REFERENCES workers(worker_id),
    location          TEXT,
    worker_type       TEXT NOT NULL CHECK
                      (worker_type IN ('employee','contractor','intern')),
    status            TEXT NOT NULL CHECK
                      (status IN ('pre_hire','active','leave','terminated')),
    hire_date         TEXT,
    start_date        TEXT,
    termination_date  TEXT,
    leave_start       TEXT,
    leave_end         TEXT,
    updated_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workers_updated ON workers(updated_at);
CREATE INDEX IF NOT EXISTS idx_workers_status  ON workers(status);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def days(n):
    """Date n days from today, as YYYY-MM-DD. Negative is the past."""
    d = datetime.date.today() + datetime.timedelta(days=n)
    return d.isoformat()


# --------------------------------------------------------------------------
# Seed set: 15 workers, 5 departments, 3-level manager chain.
# Edge cases are seeded now so they're waiting when Phase 3 arrives.
# --------------------------------------------------------------------------

SEED = [
    # --- leadership: top of the manager chain ---
    dict(worker_id="W0001", employee_id="100001",
         first_name="Dana", last_name="Whitfield",
         department="Executive", cost_center="CC-100",
         job_title="Chief Operating Officer", manager_worker_id=None,
         location="Nashville", worker_type="employee", status="active",
         start_date=days(-1800)),

    # --- level 2: department heads ---
    dict(worker_id="W0002", employee_id="100002",
         first_name="Marcus", last_name="Oyelaran",
         department="Engineering", cost_center="CC-200",
         job_title="Director of Engineering", manager_worker_id="W0001",
         location="Nashville", worker_type="employee", status="active",
         start_date=days(-1200)),

    dict(worker_id="W0003", employee_id="100003",
         first_name="Priya", last_name="Raghunathan",
         department="Sales", cost_center="CC-300",
         job_title="Director of Sales", manager_worker_id="W0001",
         location="Chicago", worker_type="employee", status="active",
         start_date=days(-1100)),

    dict(worker_id="W0004", employee_id="100004",
         first_name="Eleni", last_name="Papadopoulou",
         department="Finance", cost_center="CC-400",
         job_title="Controller", manager_worker_id="W0001",
         location="Nashville", worker_type="employee", status="active",
         start_date=days(-900)),

    # --- level 3: managers ---
    dict(worker_id="W0005", employee_id="100005",
         first_name="Tomas", last_name="Brennan",
         department="Engineering", cost_center="CC-200",
         job_title="Engineering Manager", manager_worker_id="W0002",
         location="Nashville", worker_type="employee", status="active",
         start_date=days(-700)),

    dict(worker_id="W0006", employee_id="100006",
         first_name="Aisha", last_name="Nakamura",
         department="Operations", cost_center="CC-500",
         job_title="Operations Manager", manager_worker_id="W0001",
         location="Dallas", worker_type="employee", status="active",
         start_date=days(-650)),

    # --- level 4: individual contributors ---
    dict(worker_id="W0007", employee_id="100007",
         first_name="Daniel", last_name="Okonkwo",
         department="Engineering", cost_center="CC-200",
         job_title="Software Engineer II", manager_worker_id="W0005",
         location="Nashville", worker_type="employee", status="active",
         start_date=days(-400)),

    # EDGE: UPN collision with W0009 (both resolve to d.okonkwo)
    dict(worker_id="W0008", employee_id="100008",
         first_name="Delphine", last_name="Okonkwo",
         department="Sales", cost_center="CC-300",
         job_title="Account Executive", manager_worker_id="W0003",
         location="Chicago", worker_type="employee", status="active",
         start_date=days(-380)),

    # EDGE: diacritics requiring transliteration
    dict(worker_id="W0009", employee_id="100009",
         first_name="Zoë", last_name="Müller-Søndergaard",
         department="Finance", cost_center="CC-400",
         job_title="Senior Analyst", manager_worker_id="W0004",
         location="Nashville", worker_type="employee", status="active",
         start_date=days(-300)),

    # EDGE: contractor with NO employee_id. Correlation must fall back
    # to worker_id or this person gets a duplicate account every run.
    dict(worker_id="W0010", employee_id=None,
         first_name="Victor", last_name="Almeida",
         department="Engineering", cost_center="CC-200",
         job_title="Contract Developer", manager_worker_id="W0005",
         location="Remote", worker_type="contractor", status="active",
         start_date=days(-120)),

    # EDGE: future-dated hire. Pre-stage disabled, enable on start date.
    dict(worker_id="W0011", employee_id="100011",
         first_name="Harriet", last_name="Vance",
         department="Sales", cost_center="CC-300",
         job_title="Sales Engineer", manager_worker_id="W0003",
         location="Chicago", worker_type="employee", status="pre_hire",
         start_date=days(14)),

    # EDGE: intern, short tenure, different birthright access
    dict(worker_id="W0012", employee_id="100012",
         first_name="Jun", last_name="Park",
         department="Engineering", cost_center="CC-200",
         job_title="Engineering Intern", manager_worker_id="W0005",
         location="Nashville", worker_type="intern", status="active",
         start_date=days(-60)),

    # EDGE: on leave. Suspend, snapshot entitlements, restore on return.
    dict(worker_id="W0013", employee_id="100013",
         first_name="Rosa", last_name="Iglesias",
         department="Operations", cost_center="CC-500",
         job_title="Logistics Coordinator", manager_worker_id="W0006",
         location="Dallas", worker_type="employee", status="leave",
         start_date=days(-500), leave_start=days(-30), leave_end=days(45)),

    # EDGE: terminated, and employee_id 100014 is reused by W0015 below.
    dict(worker_id="W0014", employee_id="100014",
         first_name="Gregory", last_name="Lindqvist",
         department="Operations", cost_center="CC-500",
         job_title="Warehouse Lead", manager_worker_id="W0006",
         location="Dallas", worker_type="employee", status="terminated",
         start_date=days(-800), termination_date=days(-45)),

    # EDGE: same employee_id as the terminated W0014. Correlating on
    # employee_id hands this new hire the old worker's account.
    dict(worker_id="W0015", employee_id="100014",
         first_name="Nadia", last_name="Haddad",
         department="Operations", cost_center="CC-500",
         job_title="Warehouse Lead", manager_worker_id="W0006",
         location="Dallas", worker_type="employee", status="active",
         start_date=days(-10)),
]

COLUMNS = [
    "worker_id", "employee_id", "first_name", "last_name", "preferred_name",
    "legal_name", "department", "cost_center", "job_title",
    "manager_worker_id", "location", "worker_type", "status", "hire_date",
    "start_date", "termination_date", "leave_start", "leave_end",
    "updated_at",
]


def init(reset=False):
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
        print(f"[-] deleted {DB_PATH.name}")

    conn = connect()
    conn.executescript(SCHEMA)

    existing = conn.execute("SELECT COUNT(*) AS n FROM workers").fetchone()["n"]
    if existing:
        print(f"[=] {existing} workers already present, not seeding")
        conn.close()
        return

    stamp = now()
    rows = []
    for w in SEED:
        record = {c: None for c in COLUMNS}
        record.update(w)
        record["hire_date"] = record["hire_date"] or record["start_date"]
        record["legal_name"] = (
            record["legal_name"]
            or f"{record['first_name']} {record['last_name']}"
        )
        record["updated_at"] = stamp
        rows.append(tuple(record[c] for c in COLUMNS))

    placeholders = ",".join("?" * len(COLUMNS))
    conn.executemany(
        f"INSERT INTO workers ({','.join(COLUMNS)}) VALUES ({placeholders})",
        rows,
    )
    conn.commit()
    print(f"[+] seeded {len(rows)} workers into {DB_PATH.name}")

    summary = conn.execute(
        "SELECT status, COUNT(*) AS n FROM workers GROUP BY status"
    ).fetchall()
    for row in summary:
        print(f"    {row['status']}: {row['n']}")
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="delete the database and reseed from scratch")
    args = parser.parse_args()
    init(reset=args.reset)
