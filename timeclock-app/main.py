"""
main.py - Contoso Timeclock, a legacy time-tracking app.

It does NOT speak SCIM. Entra cannot provision it. The sync engine has to
drive it directly through a custom connector.

Deliberate legacy quirks, so the connector has real work to do:
  - Its own field names:   login, full_name, dept, is_active
  - API key header         X-Api-Key, not a bearer token
  - Its own number IDs     emp_no: 42, assigned by the app
  - Full-replace updates   PUT must send login, full_name AND dept
  - No delete              only /deactivate and /activate
  - No external ID field   the app cannot store your worker ID
  - Small pages            at most 5 employees per page, so the connector
                           must page through results or miss people
  - Its own error format   {"error": "...", "message": "..."}

Run (from inside timeclock-app/):
    uvicorn main:app --reload --port 8003
"""

import datetime
import json
import os
import pathlib
import sqlite3

from dotenv import load_dotenv
from fastapi import FastAPI, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ROOT = pathlib.Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

API_KEY = os.environ.get("TIMECLOCK_API_KEY")
DB_PATH = pathlib.Path(__file__).parent / "timeclock.db"
PAGE_MAX = 5

app = FastAPI(title="Contoso Timeclock (legacy API)")


class NewEmployee(BaseModel):
    login: str
    full_name: str
    dept: str
    is_active: bool = True


class EmployeeUpdate(BaseModel):
    """Full replace. All three fields are required, even unchanged ones."""
    login: str
    full_name: str
    dept: str


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            emp_no          INTEGER PRIMARY KEY AUTOINCREMENT,
            login           TEXT NOT NULL UNIQUE COLLATE NOCASE,
            full_name       TEXT NOT NULL,
            dept            TEXT NOT NULL,
            is_active       INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            deactivated_at  TEXT
        )
    """)
    return conn


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(event, **fields):
    print(json.dumps({"ts": now(), "app": "timeclock", "event": event,
                      **fields}), flush=True)


def error(status, code, message):
    """Legacy error shape. Not SCIM, not FastAPI's default."""
    return JSONResponse({"error": code, "message": message},
                        status_code=status)


def bad_key(x_api_key):
    return not API_KEY or x_api_key != API_KEY


def as_dict(row):
    return {
        "emp_no": row["emp_no"],
        "login": row["login"],
        "full_name": row["full_name"],
        "dept": row["dept"],
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "deactivated_at": row["deactivated_at"],
    }


@app.get("/api/health")
def health():
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM employees WHERE is_active = 1").fetchone()[0]
    conn.close()
    return {"status": "ok", "employees": total, "active": active,
            "inactive": total - active, "key_configured": bool(API_KEY)}


@app.get("/api/employees")
def list_employees(offset: int = Query(0, ge=0),
                   limit: int = Query(PAGE_MAX, ge=1),
                   x_api_key: str = Header(None)):
    """Never more than PAGE_MAX per page, whatever the caller asks for."""
    if bad_key(x_api_key):
        return error(401, "BAD_KEY", "missing or invalid X-Api-Key")
    limit = min(limit, PAGE_MAX)
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    rows = conn.execute(
        "SELECT * FROM employees ORDER BY emp_no LIMIT ? OFFSET ?",
        (limit, offset)).fetchall()
    conn.close()
    log("list", offset=offset, returned=len(rows), total=total)
    return {"total": total, "offset": offset, "limit": limit,
            "items": [as_dict(r) for r in rows]}


@app.get("/api/employees/{emp_no}")
def get_employee(emp_no: int, x_api_key: str = Header(None)):
    if bad_key(x_api_key):
        return error(401, "BAD_KEY", "missing or invalid X-Api-Key")
    conn = db()
    row = conn.execute("SELECT * FROM employees WHERE emp_no = ?",
                       (emp_no,)).fetchone()
    conn.close()
    if not row:
        return error(404, "NOT_FOUND", f"no employee {emp_no}")
    return as_dict(row)


@app.post("/api/employees", status_code=201)
def create_employee(body: NewEmployee, x_api_key: str = Header(None)):
    if bad_key(x_api_key):
        return error(401, "BAD_KEY", "missing or invalid X-Api-Key")
    conn = db()
    if conn.execute("SELECT 1 FROM employees WHERE login = ?",
                    (body.login,)).fetchone():
        conn.close()
        log("create_conflict", login=body.login)
        return error(409, "LOGIN_EXISTS", f"login {body.login} is taken")
    stamp = now()
    cur = conn.execute(
        "INSERT INTO employees (login, full_name, dept, is_active, "
        "created_at, updated_at, deactivated_at) VALUES (?,?,?,?,?,?,?)",
        (body.login, body.full_name, body.dept, int(body.is_active),
         stamp, stamp, None if body.is_active else stamp))
    conn.commit()
    row = conn.execute("SELECT * FROM employees WHERE emp_no = ?",
                       (cur.lastrowid,)).fetchone()
    conn.close()
    log("create", emp_no=row["emp_no"], login=body.login,
        is_active=body.is_active)
    return JSONResponse(as_dict(row), status_code=201)


@app.put("/api/employees/{emp_no}")
def replace_employee(emp_no: int, body: EmployeeUpdate,
                     x_api_key: str = Header(None)):
    """
    Full replace of login, full_name and dept. Leave one out and the
    request is rejected. Active status is NOT changed here; that has its
    own endpoints.
    """
    if bad_key(x_api_key):
        return error(401, "BAD_KEY", "missing or invalid X-Api-Key")
    conn = db()
    row = conn.execute("SELECT * FROM employees WHERE emp_no = ?",
                       (emp_no,)).fetchone()
    if not row:
        conn.close()
        return error(404, "NOT_FOUND", f"no employee {emp_no}")
    clash = conn.execute(
        "SELECT emp_no FROM employees WHERE login = ? AND emp_no != ?",
        (body.login, emp_no)).fetchone()
    if clash:
        conn.close()
        return error(409, "LOGIN_EXISTS", f"login {body.login} is taken")
    conn.execute(
        "UPDATE employees SET login = ?, full_name = ?, dept = ?, "
        "updated_at = ? WHERE emp_no = ?",
        (body.login, body.full_name, body.dept, now(), emp_no))
    conn.commit()
    row = conn.execute("SELECT * FROM employees WHERE emp_no = ?",
                       (emp_no,)).fetchone()
    conn.close()
    log("replace", emp_no=emp_no, login=body.login, dept=body.dept)
    return as_dict(row)


def set_active(emp_no, active, x_api_key):
    if bad_key(x_api_key):
        return error(401, "BAD_KEY", "missing or invalid X-Api-Key")
    conn = db()
    row = conn.execute("SELECT * FROM employees WHERE emp_no = ?",
                       (emp_no,)).fetchone()
    if not row:
        conn.close()
        return error(404, "NOT_FOUND", f"no employee {emp_no}")
    if bool(row["is_active"]) != active:
        stamp = now()
        conn.execute(
            "UPDATE employees SET is_active = ?, updated_at = ?, "
            "deactivated_at = ? WHERE emp_no = ?",
            (int(active), stamp, None if active else stamp, emp_no))
        conn.commit()
        log("activate" if active else "deactivate", emp_no=emp_no,
            login=row["login"])
    row = conn.execute("SELECT * FROM employees WHERE emp_no = ?",
                       (emp_no,)).fetchone()
    conn.close()
    return as_dict(row)


@app.post("/api/employees/{emp_no}/deactivate")
def deactivate(emp_no: int, x_api_key: str = Header(None)):
    return set_active(emp_no, False, x_api_key)


@app.post("/api/employees/{emp_no}/activate")
def activate(emp_no: int, x_api_key: str = Header(None)):
    return set_active(emp_no, True, x_api_key)
