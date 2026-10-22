#!/usr/bin/env python3
"""
engine.py — the sync engine. Reads HR, reads Entra, makes Entra agree.

    desired_state()   what HR implies Entra should look like
    live_state()      what Entra actually looks like
    diff()            an ordered list of operations
    apply()           executes them

Plan-only is the default. Writing requires --apply, explicitly.

Every operation is stamped with the run's correlation id and appended
to logs/audit-<date>.jsonl. One HR event traced from feed through Graph
write is the centerpiece artifact of this project; the correlation id
is what makes that trace possible.

Usage:
    python engine.py                      plan only (default)
    python engine.py --apply              execute the plan
    python engine.py --delta              only workers changed since last run
    python engine.py --delta --apply
    python engine.py --json               plan as JSON
    python engine.py --reset-cursor       forget the delta position
"""

import argparse
import datetime
import json
import pathlib
import secrets
import string
import sys
import time
import uuid

import requests

import graph
import upn as upn_module

ROOT = pathlib.Path(__file__).parent.parent
STATE_PATH = ROOT / "state" / "tenant-objects.json"
CURSOR_PATH = ROOT / "state" / "sync-cursor.json"
LOG_DIR = ROOT / "logs"
HR_BASE = "http://127.0.0.1:8001"

# Attributes the engine owns. Anything not listed, it does not touch.
# HR owns title and department; IT owns mail and proxyAddresses.
# Neither overwrites the other — attribute authority, encoded.
MANAGED_ATTRIBUTES = [
    "displayName", "givenName", "surname", "department", "jobTitle",
    "employeeId", "employeeType", "companyName", "accountEnabled",
]

# Creates must land before anything that references the created object.
OP_ORDER = {
    "CREATE_USER": 0,
    "UPDATE_USER": 1,
    "SET_MANAGER": 2,
    "ADD_GROUP_MEMBER": 3,
    "DISABLE_USER": 4,
    "ORPHAN": 9,
}


class Op:
    def __init__(self, kind, worker_id, detail, payload=None):
        self.kind = kind
        self.worker_id = worker_id
        self.detail = detail
        self.payload = payload or {}

    def __repr__(self):
        return f"{self.kind:<18} {self.worker_id:<7} {self.detail}"

    def to_dict(self):
        return {"kind": self.kind, "worker_id": self.worker_id,
                "detail": self.detail, "payload": self.payload}


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

class Audit:
    """
    Append-only JSON lines. One record per operation attempt, success or
    failure, every one carrying the same correlation id.
    """

    def __init__(self, correlation_id):
        self.correlation_id = correlation_id
        LOG_DIR.mkdir(exist_ok=True)
        day = datetime.date.today().isoformat()
        self.path = LOG_DIR / f"audit-{day}.jsonl"

    def record(self, **fields):
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "correlation_id": self.correlation_id,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def fetch_hr(since=None):
    params = {"since": since} if since else {}
    try:
        resp = requests.get(f"{HR_BASE}/workers", params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        sys.exit(f"HR API unreachable at {HR_BASE} — is uvicorn running?\n{exc}")
    return resp.json()


def live_state(extension):
    """Every Entra user the engine owns, keyed by workerId, plus all UPNs."""
    select = ",".join([
        "id", "userPrincipalName", "displayName", "givenName", "surname",
        "department", "jobTitle", "employeeId", "employeeType",
        "companyName", "accountEnabled", extension,
    ])
    users = graph.collect(f"/users?$select={select}&$top=999")

    owned, all_upns = {}, set()
    for user in users:
        all_upns.add(user["userPrincipalName"].lower())
        worker_id = user.get(extension)
        if worker_id:
            owned.setdefault(worker_id, []).append(user)
    return owned, all_upns


def read_cursor():
    if CURSOR_PATH.exists():
        return json.loads(
            CURSOR_PATH.read_text(encoding="utf-8")
        ).get("last_server_time")
    return None


def write_cursor(server_time):
    CURSOR_PATH.parent.mkdir(exist_ok=True)
    CURSOR_PATH.write_text(
        json.dumps({
            "last_server_time": server_time,
            "written_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
        }, indent=2),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# desired state
# --------------------------------------------------------------------------

def should_have_account(worker):
    return worker["status"] in ("pre_hire", "active", "leave")


def should_be_enabled(worker, today):
    if worker["status"] != "active":
        return False
    start = worker.get("start_date")
    return not start or start <= today


def desired_user(worker, today):
    first = worker["preferred_name"] or worker["first_name"]
    return {
        "displayName": f"{first} {worker['last_name']}",
        "givenName": worker["first_name"],
        "surname": worker["last_name"],
        "department": worker["department"],
        "jobTitle": worker["job_title"],
        "employeeId": worker["employee_id"],
        "employeeType": worker["worker_type"],
        "companyName": "Contoso",
        "accountEnabled": should_be_enabled(worker, today),
    }


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

def diff(workers, owned, all_upns, cfg, state, full_snapshot=True):
    today = datetime.date.today().isoformat()
    ops, halts = [], []

    taken = set(all_upns)
    by_worker_id = {w["worker_id"]: w for w in workers}

    for worker in sorted(workers, key=lambda w: w["worker_id"]):
        wid = worker["worker_id"]
        matches = owned.get(wid, [])

        if len(matches) > 1:
            halts.append(
                f"{wid}: {len(matches)} Entra users carry this workerId "
                f"({', '.join(m['userPrincipalName'] for m in matches)}). "
                "Ambiguous correlation never guesses."
            )
            continue

        if not should_have_account(worker):
            if matches and matches[0].get("accountEnabled"):
                ops.append(Op("DISABLE_USER", wid,
                              f"{matches[0]['userPrincipalName']} "
                              f"(status={worker['status']})",
                              {"id": matches[0]["id"]}))
            continue

        if not matches:
            try:
                new_upn, collided = upn_module.generate(
                    worker["first_name"], worker["last_name"],
                    cfg["upn_suffix"], taken,
                )
            except (ValueError, RuntimeError) as exc:
                halts.append(f"{wid}: {exc}")
                continue

            taken.add(new_upn)
            attrs = desired_user(worker, today)
            note = "" if not collided else "  [UPN collision resolved]"
            ops.append(Op(
                "CREATE_USER", wid,
                f"{new_upn}  enabled={attrs['accountEnabled']}{note}",
                {"userPrincipalName": new_upn,
                 "mailNickname": upn_module.mail_nickname(new_upn),
                 "usageLocation": "US",
                 "upn_collision": collided,
                 **attrs},
            ))

            if worker.get("manager_worker_id"):
                ops.append(Op("SET_MANAGER", wid,
                              f"-> {worker['manager_worker_id']}",
                              {"manager_worker_id":
                               worker["manager_worker_id"]}))

            if state.get("birthright_group_id"):
                ops.append(Op("ADD_GROUP_MEMBER", wid, "IDP-All-Workers",
                              {"group_id": state["birthright_group_id"]}))
            continue

        existing = matches[0]
        desired = desired_user(worker, today)
        drift = {k: v for k, v in desired.items() if existing.get(k) != v}
        if drift:
            summary = ", ".join(
                f"{k}: {existing.get(k)!r} -> {v!r}" for k, v in drift.items()
            )
            ops.append(Op("UPDATE_USER", wid,
                          f"{existing['userPrincipalName']}  {summary}",
                          {"id": existing["id"], **drift}))

    # Orphan detection needs the whole HR population. A delta only contains
    # workers who changed, so everyone else would look like they had left.
    # A delta can prove presence; only a full snapshot can prove absence.
    if full_snapshot:
        for worker_id, users in owned.items():
            if worker_id not in by_worker_id:
                for user in users:
                    ops.append(Op("ORPHAN", worker_id,
                                  f"{user['userPrincipalName']} has a "
                                  "workerId with no matching HR record"))

    ops.sort(key=lambda op: (OP_ORDER.get(op.kind, 5), op.worker_id))
    return ops, halts


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def strong_password():
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(24))


def with_replication_retry(fn, attempts=6, delay=10):
    """
    A 201 from POST /users does not mean the object is addressable yet.
    Graph returns 404 Request_ResourceNotFound against a brand-new id
    until replication catches up. Retry that case only; anything else
    is a real failure and should surface immediately.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except graph.GraphError as exc:
            if exc.status == 404 and attempt < attempts - 1:
                time.sleep(delay)
                continue
            raise


def resolve_entra_id(worker_id, created, owned, extension):
    """
    Manager references are worker ids at plan time, because the Entra
    object may not exist yet. Resolve them here, after the creates.
    """
    if worker_id in created:
        return created[worker_id]
    if worker_id in owned:
        return owned[worker_id][0]["id"]
    found = graph.collect(
        f"/users?$filter={extension} eq '{worker_id}'"
        "&$select=id&$count=true"
    )
    return found[0]["id"] if found else None


def apply(ops, cfg, owned, audit):
    created = {}
    done = failed = 0

    for op in ops:
        if op.kind == "ORPHAN":
            audit.record(op="ORPHAN", worker_id=op.worker_id,
                         status="reported", detail=op.detail)
            print(f"  ?? {op}")
            continue

        try:
            if op.kind == "CREATE_USER":
                body = {k: v for k, v in op.payload.items()
                        if k != "upn_collision"}
                body["passwordProfile"] = {
                    "forceChangePasswordNextSignIn": True,
                    "password": strong_password(),
                }
                body[cfg["extension"]] = op.worker_id
                user = graph.call("POST", "/users", body)
                created[op.worker_id] = user["id"]
                audit.record(op=op.kind, worker_id=op.worker_id,
                             status="success", entra_id=user["id"],
                             upn=op.payload["userPrincipalName"],
                             upn_collision=op.payload.get("upn_collision"))

            elif op.kind == "UPDATE_USER":
                body = {k: v for k, v in op.payload.items() if k != "id"}
                graph.call("PATCH", f"/users/{op.payload['id']}", body)
                audit.record(op=op.kind, worker_id=op.worker_id,
                             status="success", entra_id=op.payload["id"],
                             changed=list(body))

            elif op.kind == "DISABLE_USER":
                graph.call("PATCH", f"/users/{op.payload['id']}",
                           {"accountEnabled": False})
                graph.call(
                    "POST",
                    f"/users/{op.payload['id']}/revokeSignInSessions")
                audit.record(op=op.kind, worker_id=op.worker_id,
                             status="success", entra_id=op.payload["id"])

            elif op.kind == "SET_MANAGER":
                user_id = resolve_entra_id(op.worker_id, created, owned,
                                           cfg["extension"])
                mgr_id = resolve_entra_id(op.payload["manager_worker_id"],
                                          created, owned, cfg["extension"])
                if not user_id or not mgr_id:
                    audit.record(op=op.kind, worker_id=op.worker_id,
                                 status="deferred",
                                 detail="manager not provisioned yet")
                    print(f"  .. {op.kind} {op.worker_id} deferred "
                          "(manager not yet in Entra)")
                    continue
                with_replication_retry(lambda: graph.call(
                    "PUT", f"/users/{user_id}/manager/$ref",
                    {"@odata.id":
                     f"https://graph.microsoft.com/v1.0/users/{mgr_id}"},
                ))
                audit.record(op=op.kind, worker_id=op.worker_id,
                             status="success", entra_id=user_id,
                             manager_entra_id=mgr_id)

            elif op.kind == "ADD_GROUP_MEMBER":
                user_id = resolve_entra_id(op.worker_id, created, owned,
                                           cfg["extension"])
                gid = op.payload["group_id"]
                try:
                    with_replication_retry(lambda: graph.call(
                        "POST", f"/groups/{gid}/members/$ref",
                        {"@odata.id":
                         "https://graph.microsoft.com/v1.0/"
                         f"directoryObjects/{user_id}"},
                    ))
                except graph.GraphError as exc:
                    # Already a member — the server enforcing idempotency.
                    if exc.status != 400 or "already exist" not in exc.body:
                        raise
                audit.record(op=op.kind, worker_id=op.worker_id,
                             status="success", entra_id=user_id,
                             group_id=gid)

            done += 1
            print(f"  ok {op}")

        except graph.GraphError as exc:
            failed += 1
            audit.record(op=op.kind, worker_id=op.worker_id,
                         status="failed", http_status=exc.status,
                         error=exc.body[:400])
            print(f"  !! {op.kind} {op.worker_id} FAILED {exc.status}")
            print(f"     {exc.body[:300]}")

    return done, failed


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="execute the plan (default is plan only)")
    parser.add_argument("--delta", action="store_true",
                        help="only workers changed since the stored cursor")
    parser.add_argument("--since", default=None,
                        help="explicit timestamp, overrides --delta")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--reset-cursor", action="store_true")
    args = parser.parse_args()

    if args.reset_cursor:
        if CURSOR_PATH.exists():
            CURSOR_PATH.unlink()
            print("cursor reset")
        else:
            print("no cursor to reset")
        return

    correlation_id = str(uuid.uuid4())
    audit = Audit(correlation_id)
    cfg = graph.config()

    if not STATE_PATH.exists():
        sys.exit("state/tenant-objects.json missing — run bootstrap-tenant.py")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    since = args.since or (read_cursor() if args.delta else None)
    hr = fetch_hr(since)
    workers = hr["workers"]
    owned, all_upns = live_state(cfg["extension"])
    ops, halts = diff(workers, owned, all_upns, cfg, state,
                      full_snapshot=since is None)

    if args.json:
        print(json.dumps({
            "correlation_id": correlation_id,
            "hr_server_time": hr["server_time"],
            "since": since,
            "operations": [op.to_dict() for op in ops],
            "halts": halts,
        }, indent=2))
        return

    print(f"correlation id : {correlation_id}")
    print(f"mode           : {'APPLY' if args.apply else 'plan only'}")
    print(f"since          : {since or '(full snapshot)'}")
    print(f"hr workers     : {len(workers)}")
    print(f"entra owned    : {sum(len(v) for v in owned.values())}")
    print(f"audit log      : {audit.path.relative_to(ROOT)}\n")

    if halts:
        print("HALTS — refusals, not plans:\n")
        for halt in halts:
            print(f"  !! {halt}")
            audit.record(op="HALT", status="halted", detail=halt)
        print()

    if not ops:
        print("no changes. Entra matches HR.")
        if args.apply:
            write_cursor(hr["server_time"])
        return

    counts = {}
    for op in ops:
        counts[op.kind] = counts.get(op.kind, 0) + 1
    print("plan: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print()

    if not args.apply:
        for op in ops:
            print(f"  {op}")
        print(f"\n{len(ops)} operations. Nothing written — "
              "pass --apply to execute.")
        return

    print("applying:\n")
    done, failed = apply(ops, cfg, owned, audit)
    print(f"\n{done} succeeded, {failed} failed")

    if failed == 0:
        write_cursor(hr["server_time"])
        print(f"cursor advanced to {hr['server_time']}")
    else:
        print("cursor NOT advanced — failures would be skipped next run")

    print(f"\ntrace this run:\n  Select-String -Path "
          f"{audit.path.relative_to(ROOT)} -Pattern {correlation_id}")


if __name__ == "__main__":
    main()
