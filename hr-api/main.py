"""
main.py — the mock HR system. FastAPI over SQLite.

This is the source of truth. The sync engine reads it and makes Entra
agree with it. Postman mutates it to fire lifecycle events.

Two read endpoints by design:
  GET /workers              full snapshot  -> reconciliation runs on this
  GET /workers?since=<ts>   delta          -> the normal cycle runs on this

No webhooks. A real HR feed doesn't have them, and building the engine
against a polled feed is the point.

Run:
    uvicorn main:app --reload --port 8001     (from inside hr-api/)

Docs at http://127.0.0.1:8001/docs
"""

import datetime
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from db import COLUMNS, connect, init, now

app = FastAPI(
    title="Mock HR System",
    description="Source of truth for the identity platform.",
    version="1.0.0",
)

WorkerType = Literal["employee", "contractor", "intern"]
Status = Literal["pre_hire", "active", "leave", "terminated"]


class WorkerIn(BaseModel):
    worker_id: str = Field(..., examples=["W0016"])
    employee_id: Optional[str] = None
    first_name: str
    last_name: str
    preferred_name: Optional[str] = None
    legal_name: Optional[str] = None
    department: str
    cost_center: Optional[str] = None
    job_title: Optional[str] = None
    manager_worker_id: Optional[str] = None
    location: Optional[str] = None
    worker_type: WorkerType = "employee"
    status: Status = "active"
    hire_date: Optional[str] = None
    start_date: Optional[str] = None
    termination_date: Optional[str] = None
    leave_start: Optional[str] = None
    leave_end: Optional[str] = None


class WorkerPatch(BaseModel):
    """Every field optional. Only what's sent gets changed."""
    employee_id: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    preferred_name: Optional[str] = None
    legal_name: Optional[str] = None
    department: Optional[str] = None
    cost_center: Optional[str] = None
    job_title: Optional[str] = None
    manager_worker_id: Optional[str] = None
    location: Optional[str] = None
    worker_type: Optional[WorkerType] = None
    status: Optional[Status] = None
    hire_date: Optional[str] = None
    start_date: Optional[str] = None
    termination_date: Optional[str] = None
    leave_start: Optional[str] = None
    leave_end: Optional[str] = None


@app.on_event("startup")
def startup():
    init()


def row_to_dict(row):
    return {k: row[k] for k in row.keys()}


@app.get("/health")
def health():
    conn = connect()
    n = conn.execute("SELECT COUNT(*) AS n FROM workers").fetchone()["n"]
    conn.close()
    return {"status": "ok", "workers": n, "server_time": now()}


@app.get("/workers")
def list_workers(
    since: Optional[str] = Query(
        None,
        description="ISO timestamp. Returns only workers changed after it.",
        examples=["2026-10-20T16:00:00+00:00"],
    ),
    status: Optional[Status] = Query(None),
    department: Optional[str] = Query(None),
):
    """
    No `since` -> full snapshot (reconciliation).
    With `since` -> delta (normal sync cycle).

    The response carries `server_time`. The engine stores that and sends
    it back as `since` on the next run. Never use the client's own clock
    for this — clock skew silently drops events.
    """
    sql = "SELECT * FROM workers WHERE 1=1"
    params = []
    if since:
        sql += " AND updated_at > ?"
        params.append(since)
    if status:
        sql += " AND status = ?"
        params.append(status)
    if department:
        sql += " AND department = ?"
        params.append(department)
    sql += " ORDER BY worker_id"

    conn = connect()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return {
        "server_time": now(),
        "since": since,
        "count": len(rows),
        "workers": [row_to_dict(r) for r in rows],
    }


@app.get("/workers/{worker_id}")
def get_worker(worker_id: str):
    conn = connect()
    row = conn.execute(
        "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, f"no worker {worker_id}")
    return row_to_dict(row)


@app.post("/workers", status_code=201)
def create_worker(worker: WorkerIn):
    """New hire. This is the event the joiner path reacts to."""
    conn = connect()
    exists = conn.execute(
        "SELECT 1 FROM workers WHERE worker_id = ?", (worker.worker_id,)
    ).fetchone()
    if exists:
        conn.close()
        raise HTTPException(409, f"{worker.worker_id} already exists")

    record = {c: None for c in COLUMNS}
    record.update(worker.model_dump())
    record["hire_date"] = record["hire_date"] or record["start_date"]
    record["legal_name"] = (
        record["legal_name"] or f"{record['first_name']} {record['last_name']}"
    )
    record["updated_at"] = now()

    placeholders = ",".join("?" * len(COLUMNS))
    conn.execute(
        f"INSERT INTO workers ({','.join(COLUMNS)}) VALUES ({placeholders})",
        tuple(record[c] for c in COLUMNS),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM workers WHERE worker_id = ?", (worker.worker_id,)
    ).fetchone()
    conn.close()
    return row_to_dict(row)


@app.patch("/workers/{worker_id}")
def update_worker(worker_id: str, patch: WorkerPatch):
    """
    Mover, leaver, leave of absence, conversion — all of them are a PATCH.
    The engine infers what happened by diffing, not by being told.
    """
    changes = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(400, "no fields to update")

    conn = connect()
    exists = conn.execute(
        "SELECT 1 FROM workers WHERE worker_id = ?", (worker_id,)
    ).fetchone()
    if not exists:
        conn.close()
        raise HTTPException(404, f"no worker {worker_id}")

    changes["updated_at"] = now()
    assignments = ", ".join(f"{k} = ?" for k in changes)
    conn.execute(
        f"UPDATE workers SET {assignments} WHERE worker_id = ?",
        (*changes.values(), worker_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
    ).fetchone()
    conn.close()
    return row_to_dict(row)


@app.post("/workers/{worker_id}/terminate")
def terminate_worker(worker_id: str, effective: Optional[str] = None):
    """
    Convenience wrapper over PATCH. A backdated `effective` is a real
    scenario: revoke now, then report the exposure window.
    """
    effective = effective or datetime.date.today().isoformat()
    return update_worker(
        worker_id,
        WorkerPatch(status="terminated", termination_date=effective),
    )
