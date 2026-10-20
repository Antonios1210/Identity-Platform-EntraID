#!/usr/bin/env python3
"""
engine.py — the sync engine. Reads HR, reads Entra, prints the difference.

Session 3 scope: READ ONLY. Computes a plan and prints it. Nothing is
written to the tenant. Apply arrives in Session 4.

The shape is deliberately the same as terraform plan/apply:

    desired_state()   what HR implies Entra should look like
    live_state()      what Entra actually looks like
    diff()            an ordered list of operations
    apply()           executes them (Session 4)

Every scenario in the Phase 3 matrix becomes assertable because the
plan is a value you can inspect without touching the tenant.

Usage:
    python sync-engine/engine.py --whatif
    python sync-engine/engine.py --whatif --json
"""

import argparse
import datetime
import json
import pathlib
import sys
import uuid

import requests

import graph
import upn as upn_module

ROOT = pathlib.Path(__file__).parent.parent
STATE_PATH = ROOT / "state" / "tenant-objects.json"
HR_BASE = "http://127.0.0.1:8001"

# Attributes the engine owns. Anything not listed here, it does not touch.
# HR owns title and department; IT owns mail and proxyAddresses. Neither
# overwrites the other - that is the attribute authority rule, encoded.
MANAGED_ATTRIBUTES = [
    "displayName", "givenName", "surname", "department", "jobTitle",
    "employeeId", "employeeType", "companyName", "accountEnabled",
]


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------

class Op:
    """One planned change. Printable, comparable, assertable."""

    def __init__(self, kind, worker_id, detail, payload=None):
        self.kind = kind
        self.worker_id = worker_id
        self.detail = detail
        self.payload = payload or {}

    def __repr__(self):
        return f"{self.kind:<18} {self.worker_id:<7} {self.detail}"

    def to_dict(self):
        return {
            "kind": self.kind,
            "worker_id": self.worker_id,
            "detail": self.detail,
            "payload": self.payload,
        }


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def fetch_hr(since=None):
    params = {"since": since} if since else {}
    try:
        resp = requests.get(f"{HR_BASE}/workers", params=params, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        sys.exit(f"HR API unreachable at {HR_BASE} - is uvicorn running?\n{exc}")
    return resp.json()


def live_state(extension):
    """
    Every Entra user the engine owns, keyed by workerId.

    Scoping on the extension is what keeps reconciliation sane. Without
    it, all 33 prebuilt tenant users look like orphans forever.

    Note: this pulls all users and filters client-side. At 33 users that
    is free. At 50,000 you would use
    $filter=<extension> ne null with $count=true, which needs
    ConsistencyLevel: eventual - already sent by graph.call.
    """
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
        if not worker_id:
            continue
        owned.setdefault(worker_id, []).append(user)
    return owned, all_upns


# --------------------------------------------------------------------------
# desired state
# --------------------------------------------------------------------------

def should_have_account(worker):
    """
    Terminated workers who never had an account do not get one.
    Pre-hires do, disabled, so the account is staged before day one.
    """
    return worker["status"] in ("pre_hire", "active", "leave")


def should_be_enabled(worker, today):
    if worker["status"] != "active":
        return False
    start = worker.get("start_date")
    return not start or start <= today


def desired_user(worker, today):
    """HR worker -> the Entra attributes the engine intends to set."""
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

def diff(workers, owned, all_upns, cfg, state):
    today = datetime.date.today().isoformat()
    ops, halts = [], []

    taken = set(all_upns)
    by_worker_id = {w["worker_id"]: w for w in workers}
    planned_creates = set()

    for worker in sorted(workers, key=lambda w: w["worker_id"]):
        wid = worker["worker_id"]
        matches = owned.get(wid, [])

        # ---- correlation ----
        if len(matches) > 1:
            halts.append(
                f"{wid}: {len(matches)} Entra users carry this workerId "
                f"({', '.join(m['userPrincipalName'] for m in matches)}). "
                "Ambiguous correlation never guesses."
            )
            continue

        if not should_have_account(worker):
            if matches:
                ops.append(Op("DISABLE_USER", wid,
                              f"{matches[0]['userPrincipalName']} "
                              f"(status={worker['status']})"))
            continue

        # ---- create ----
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
            planned_creates.add(wid)
            attrs = desired_user(worker, today)
            note = "" if not collided else "  [UPN collision resolved]"
            ops.append(Op(
                "CREATE_USER", wid,
                f"{new_upn}  enabled={attrs['accountEnabled']}{note}",
                {"userPrincipalName": new_upn,
                 "mailNickname": upn_module.mail_nickname(new_upn),
                 **attrs},
            ))

            if worker.get("manager_worker_id"):
                ops.append(Op("SET_MANAGER", wid,
                              f"-> {worker['manager_worker_id']}",
                              {"manager_worker_id":
                               worker["manager_worker_id"]}))

            if state.get("birthright_group_id"):
                ops.append(Op("ADD_GROUP_MEMBER", wid,
                              "IDP-All-Workers",
                              {"group_id": state["birthright_group_id"]}))
            continue

        # ---- update ----
        existing = matches[0]
        desired = desired_user(worker, today)
        drift = {
            key: value for key, value in desired.items()
            if existing.get(key) != value
        }
        if drift:
            summary = ", ".join(
                f"{k}: {existing.get(k)!r} -> {v!r}" for k, v in drift.items()
            )
            ops.append(Op("UPDATE_USER", wid,
                          f"{existing['userPrincipalName']}  {summary}",
                          {"id": existing["id"], **drift}))

    # ---- orphans: in Entra, not in HR ----
    for worker_id, users in owned.items():
        if worker_id not in by_worker_id:
            for user in users:
                ops.append(Op("ORPHAN", worker_id,
                              f"{user['userPrincipalName']} has a workerId "
                              "with no matching HR record"))

    return ops, halts


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--whatif", action="store_true", default=True,
                        help="print the plan without executing (default)")
    parser.add_argument("--json", action="store_true",
                        help="emit the plan as JSON")
    parser.add_argument("--since", default=None,
                        help="delta mode: only workers changed after this")
    args = parser.parse_args()

    correlation_id = str(uuid.uuid4())
    cfg = graph.config()

    if not STATE_PATH.exists():
        sys.exit("state/tenant-objects.json missing - run bootstrap-tenant.py")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    hr = fetch_hr(args.since)
    workers = hr["workers"]
    owned, all_upns = live_state(cfg["extension"])

    ops, halts = diff(workers, owned, all_upns, cfg, state)

    if args.json:
        print(json.dumps({
            "correlation_id": correlation_id,
            "hr_server_time": hr["server_time"],
            "operations": [op.to_dict() for op in ops],
            "halts": halts,
        }, indent=2))
        return

    print(f"correlation id : {correlation_id}")
    print(f"hr workers     : {len(workers)}")
    print(f"entra owned    : {sum(len(v) for v in owned.values())}")
    print(f"hr snapshot at : {hr['server_time']}\n")

    if halts:
        print("HALTS - these are not planned, they are refusals:\n")
        for halt in halts:
            print(f"  !! {halt}")
        print()

    if not ops:
        print("no changes. Entra matches HR.")
        return

    counts = {}
    for op in ops:
        counts[op.kind] = counts.get(op.kind, 0) + 1
    print("plan: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print()
    for op in ops:
        print(f"  {op}")
    print(f"\n{len(ops)} operations. Nothing was written - this is --whatif.")


if __name__ == "__main__":
    main()
