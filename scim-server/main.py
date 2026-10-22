"""
main.py — SCIM 2.0 server for "Contoso Portal", the fake downstream app.

Entra's provisioning service calls these endpoints to create, update and
disable accounts. Your code never calls them; Microsoft does.

Design decisions (yours):
  - Reachability: tunnel, not cloud. Lab project, not production.
  - Deprovisioning: soft delete first (active=false), hard delete only
    after a retention period. Wrongful terminations happen; a clinician
    locked out of a clinical app must be restorable immediately, and
    the account history must survive for audit.

Scope this session: Users only. Groups come later.

Run (from inside scim-server/):
    uvicorn main:app --reload --port 8002
"""

import datetime
import json
import os
import pathlib
import re
import sqlite3
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

ROOT = pathlib.Path(__file__).parent.parent
load_dotenv(ROOT / ".env")

DB_PATH = pathlib.Path(__file__).parent / "app.db"
TOKEN = os.environ.get("SCIM_TOKEN")

# How long a deactivated account is kept before hard delete.
# Production: driven by the longest applicable regulation (HIPAA's floor
# is 6 years). Lab: minutes, so the purge can actually be observed.
RETENTION_MINUTES = 10

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
ENTERPRISE = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

app = FastAPI(title="Contoso Portal — SCIM 2.0")


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id              TEXT PRIMARY KEY,
            user_name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
            external_id     TEXT,
            active          INTEGER NOT NULL DEFAULT 1,
            deactivated_at  TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            resource        TEXT NOT NULL
        )
    """)
    return conn


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def log(event, **fields):
    """One line per request. This is the app-side half of the trace."""
    print(json.dumps({"ts": now(), "event": event, **fields}), flush=True)


# --------------------------------------------------------------------------
# responses
# --------------------------------------------------------------------------

def scim_error(status, detail, scim_type=None):
    """RFC 7644 error body. Entra reads scimType to decide what went wrong."""
    body = {"schemas": [ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(body, status_code=status,
                        media_type="application/scim+json")


def scim_ok(body, status=200):
    return JSONResponse(body, status_code=status,
                        media_type="application/scim+json")


def normalize_manager(resource):
    """
    RFC 7643: enterprise 'manager' is a complex attribute {"value": id}.
    Entra sends it in PATCH as a bare id string. Storing that string and
    returning it later makes Entra fail to deserialize the user during
    matching - which silently blocks every update, including deactivation.
    """
    ext = resource.get(ENTERPRISE)
    if not isinstance(ext, dict) or "manager" not in ext:
        return resource
    manager = ext["manager"]
    if isinstance(manager, str):
        if manager:
            ext["manager"] = {"value": manager}
        else:
            ext.pop("manager")
    elif manager is None:
        ext.pop("manager")
    return resource


def to_resource(row):
    resource = normalize_manager(json.loads(row["resource"]))
    resource["id"] = row["id"]
    resource["active"] = bool(row["active"])
    resource["meta"] = {
        "resourceType": "User",
        "created": row["created_at"],
        "lastModified": row["updated_at"],
        "location": f"/scim/v2/Users/{row['id']}",
    }
    return resource


def authorized(authorization):
    return TOKEN and authorization == f"Bearer {TOKEN}"


# --------------------------------------------------------------------------
# PATCH path handling
#
# Entra sends updates like:
#   {"op": "Replace", "path": "active", "value": false}
#   {"op": "Replace", "path": "name.givenName", "value": "Daniel"}
#   {"op": "Replace", "path": "emails[type eq \"work\"].value", "value": "..."}
#   {"op": "Add", "path": "urn:...:enterprise:2.0:User:department", "value": "..."}
#   {"op": "Replace", "value": {"active": false}}          <- no path at all
# --------------------------------------------------------------------------

FILTERED = re.compile(r'^(\w+)\[(\w+) eq "([^"]*)"\](?:\.(\w+))?$')


def coerce(value):
    """Entra has historically sent booleans as the strings "True"/"False"."""
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def apply_path(resource, path, value, remove=False):
    value = coerce(value)

    # Enterprise extension: urn:...:enterprise:2.0:User:department
    if path.startswith(ENTERPRISE):
        attr = path[len(ENTERPRISE) + 1:]
        ext = resource.setdefault(ENTERPRISE, {})
        if remove:
            ext.pop(attr, None)
        else:
            ext[attr] = value
        return

    # Filtered multi-valued: emails[type eq "work"].value
    match = FILTERED.match(path)
    if match:
        attr, key, wanted, sub = match.groups()
        items = resource.setdefault(attr, [])
        target = next((i for i in items if i.get(key) == wanted), None)
        if remove:
            if target:
                items.remove(target)
            return
        if target is None:
            target = {key: wanted}
            items.append(target)
        if sub:
            target[sub] = value
        else:
            target.update(value if isinstance(value, dict) else {})
        return

    # Dotted: name.givenName
    if "." in path:
        parent, child = path.split(".", 1)
        node = resource.setdefault(parent, {})
        if remove:
            node.pop(child, None)
        else:
            node[child] = value
        return

    # Plain: active, displayName, title
    if remove:
        resource.pop(path, None)
    else:
        resource[path] = value


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

@app.get("/scim/v2/ServiceProviderConfig")
def service_provider_config():
    return scim_ok({
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": 200},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken",
            "name": "Bearer token",
            "description": "Static bearer token from .env",
        }],
    })


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------

@app.get("/scim/v2/Users")
def list_users(request: Request, authorization: str = Header(None)):
    """
    Entra calls this with filter=userName eq "x" before every create,
    to check whether the account already exists. Getting this wrong is
    how a SCIM app ends up with duplicates.
    """
    if not authorized(authorization):
        return scim_error(401, "invalid or missing bearer token")

    params = request.query_params
    filt = params.get("filter")
    start = max(int(params.get("startIndex", 1)), 1)
    count = min(int(params.get("count", 100)), 200)

    conn = db()
    if filt:
        m = re.match(r'^(userName|externalId) eq "([^"]*)"$', filt.strip())
        if not m:
            conn.close()
            return scim_error(400, f"unsupported filter: {filt}",
                              "invalidFilter")
        column = "user_name" if m.group(1) == "userName" else "external_id"
        rows = conn.execute(
            f"SELECT * FROM users WHERE {column} = ? COLLATE NOCASE",
            (m.group(2),),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
    conn.close()

    page = rows[start - 1:start - 1 + count]
    log("list", filter=filt, results=len(rows))
    return scim_ok({
        "schemas": [LIST_SCHEMA],
        "totalResults": len(rows),
        "startIndex": start,
        "itemsPerPage": len(page),
        "Resources": [to_resource(r) for r in page],
    })


@app.get("/scim/v2/Users/{user_id}")
def get_user(user_id: str, authorization: str = Header(None)):
    if not authorized(authorization):
        return scim_error(401, "invalid or missing bearer token")
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if not row:
        return scim_error(404, f"user {user_id} not found")
    return scim_ok(to_resource(row))


@app.post("/scim/v2/Users")
async def create_user(request: Request, authorization: str = Header(None)):
    if not authorized(authorization):
        return scim_error(401, "invalid or missing bearer token")

    body = await request.json()
    user_name = body.get("userName")
    if not user_name:
        return scim_error(400, "userName is required", "invalidValue")

    conn = db()
    if conn.execute("SELECT 1 FROM users WHERE user_name = ? COLLATE NOCASE",
                    (user_name,)).fetchone():
        conn.close()
        log("create_conflict", userName=user_name)
        return scim_error(409, f"{user_name} already exists", "uniqueness")

    user_id = str(uuid.uuid4())
    stamp = now()
    active = coerce(body.get("active", True))
    resource = normalize_manager({k: v for k, v in body.items()
                                  if k not in ("id", "meta", "active")})

    conn.execute(
        "INSERT INTO users (id, user_name, external_id, active, "
        "deactivated_at, created_at, updated_at, resource) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, user_name, body.get("externalId"), int(bool(active)),
         None if active else stamp, stamp, stamp, json.dumps(resource)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    log("create", id=user_id, userName=user_name, active=bool(active))
    return scim_ok(to_resource(row), 201)


@app.patch("/scim/v2/Users/{user_id}")
async def patch_user(user_id: str, request: Request,
                     authorization: str = Header(None)):
    if not authorized(authorization):
        return scim_error(401, "invalid or missing bearer token")

    body = await request.json()
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return scim_error(404, f"user {user_id} not found")

    resource = json.loads(row["resource"])
    resource["active"] = bool(row["active"])
    was_active = resource["active"]

    for op in body.get("Operations", []):
        kind = op.get("op", "").lower()
        path = op.get("path")
        value = op.get("value")

        if kind not in ("add", "replace", "remove"):
            conn.close()
            return scim_error(400, f"unsupported op: {kind}", "invalidSyntax")

        if path:
            apply_path(resource, path, value, remove=(kind == "remove"))
        elif isinstance(value, dict):
            for attr, attr_value in value.items():
                apply_path(resource, attr, attr_value)

    normalize_manager(resource)
    active = bool(coerce(resource.pop("active", True)))
    resource.pop("id", None)
    resource.pop("meta", None)

    # Soft delete: deactivation stamps the clock that the purge reads.
    # Reactivation clears it - a rehire or a reversed termination.
    if was_active and not active:
        deactivated_at = now()
        log("deactivate", id=user_id, userName=row["user_name"])
    elif not was_active and active:
        deactivated_at = None
        log("reactivate", id=user_id, userName=row["user_name"])
    else:
        deactivated_at = row["deactivated_at"]

    conn.execute(
        "UPDATE users SET user_name = ?, external_id = ?, active = ?, "
        "deactivated_at = ?, updated_at = ?, resource = ? WHERE id = ?",
        (resource.get("userName", row["user_name"]),
         resource.get("externalId", row["external_id"]),
         int(active), deactivated_at, now(), json.dumps(resource), user_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()

    log("patch", id=user_id, ops=len(body.get("Operations", [])))
    return scim_ok(to_resource(row))


@app.delete("/scim/v2/Users/{user_id}")
def delete_user(user_id: str, authorization: str = Header(None)):
    """
    A DELETE from Entra is honored as a soft delete, not a hard one.
    Only the retention purge ever removes a row. One rule, one place.
    """
    if not authorized(authorization):
        return scim_error(401, "invalid or missing bearer token")
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        conn.close()
        return scim_error(404, f"user {user_id} not found")
    if row["active"]:
        conn.execute(
            "UPDATE users SET active = 0, deactivated_at = ?, updated_at = ? "
            "WHERE id = ?", (now(), now(), user_id))
        conn.commit()
    conn.close()
    log("delete_as_soft", id=user_id, userName=row["user_name"])
    return JSONResponse(None, status_code=204)


# --------------------------------------------------------------------------
# retention purge — not part of SCIM, the app's own housekeeping
# --------------------------------------------------------------------------

@app.post("/admin/purge")
def purge(authorization: str = Header(None)):
    """Hard-delete accounts deactivated longer than RETENTION_MINUTES."""
    if not authorized(authorization):
        return scim_error(401, "invalid or missing bearer token")
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(minutes=RETENTION_MINUTES)).isoformat()
    conn = db()
    doomed = conn.execute(
        "SELECT id, user_name FROM users "
        "WHERE active = 0 AND deactivated_at IS NOT NULL "
        "AND deactivated_at < ?", (cutoff,)).fetchall()
    conn.execute(
        "DELETE FROM users WHERE active = 0 AND deactivated_at IS NOT NULL "
        "AND deactivated_at < ?", (cutoff,))
    conn.commit()
    conn.close()
    purged = [r["user_name"] for r in doomed]
    log("purge", retention_minutes=RETENTION_MINUTES, purged=purged)
    return {"retention_minutes": RETENTION_MINUTES, "purged": purged}


@app.get("/health")
def health():
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    active = conn.execute(
        "SELECT COUNT(*) FROM users WHERE active = 1").fetchone()[0]
    conn.close()
    return {"status": "ok", "users": total, "active": active,
            "inactive": total - active, "token_configured": bool(TOKEN)}
