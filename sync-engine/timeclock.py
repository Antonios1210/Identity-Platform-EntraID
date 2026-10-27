#!/usr/bin/env python3
"""
timeclock.py - custom connector for Contoso Timeclock.

Timeclock does not speak SCIM, so Entra cannot provision it. This
connector does what Entra does for Contoso Portal: create, update,
deactivate, read all, translate fields, and correlate accounts.

Design decisions (yours):
  1. Interface      Timeclock's own REST API
  2. Correlation    the engine keeps a link table, worker_id -> emp_no,
                    in state/timeclock-links.json. The app cannot store
                    the worker ID.
  3. Truth          HR grants access. HR OR Entra can revoke it:
                    active in Timeclock = HR says active AND the Entra
                    account is enabled. A security disable in Entra wins.
                    Logins come from the Entra username; IT owns naming.
  4. Lost links     if a worker has no link but Timeclock has an account
                    with their login, adopt it ONLY if exactly one account
                    matches and nobody else is linked to it. Otherwise
                    flag it for a human. Never guess.

Usage (from inside sync-engine/):
    python timeclock.py                       plan only
    python timeclock.py --apply               execute
    python timeclock.py --apply --crash-after-create
                                              create one account, then die
                                              before saving its link
"""

import argparse
import datetime
import json
import os
import pathlib
import sys
import time
import uuid

import requests

import graph
import upn as upn_module
from engine import Audit, fetch_hr, live_state, should_be_enabled, should_have_account

ROOT = pathlib.Path(__file__).parent.parent
LINKS_PATH = ROOT / "state" / "timeclock-links.json"
TC_BASE = "http://127.0.0.1:8003"

ORDER = {"ADOPT": 0, "CREATE": 1, "REPLACE": 2, "ACTIVATE": 3,
         "DEACTIVATE": 4, "FLAG": 8, "ORPHAN": 9}


class Op:
    def __init__(self, kind, worker_id, detail, payload=None):
        self.kind = kind
        self.worker_id = worker_id
        self.detail = detail
        self.payload = payload or {}

    def __repr__(self):
        return f"{self.kind:<11} {self.worker_id or '-':<7} {self.detail}"


# --------------------------------------------------------------------------
# talking to Timeclock
# --------------------------------------------------------------------------

class TimeclockError(RuntimeError):
    def __init__(self, method, path, status, body):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")


def tc_call(method, path, body=None, attempts=4):
    """
    One Timeclock request. Retries dropped connections and 5xx errors with
    backoff - the gap graph.py still has. A 4xx is a real answer and is
    raised immediately.
    """
    key = os.environ.get("TIMECLOCK_API_KEY")
    if not key:
        sys.exit("TIMECLOCK_API_KEY missing from .env")
    url = f"{TC_BASE}{path}"
    for attempt in range(attempts):
        try:
            resp = requests.request(method, url, json=body, timeout=30,
                                    headers={"X-Api-Key": key})
        except requests.ConnectionError:
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 500 and attempt < attempts - 1:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 400:
            raise TimeclockError(method, path, resp.status_code, resp.text)
        return resp.json()


def tc_read_all():
    """
    Every Timeclock account. The app returns at most 5 per page, so this
    MUST page; reading only the first page would make most accounts look
    nonexistent.
    """
    accounts, offset = [], 0
    while True:
        page = tc_call("GET", f"/api/employees?offset={offset}&limit=100")
        accounts.extend(page["items"])
        offset += len(page["items"])
        if not page["items"] or offset >= page["total"]:
            break
    return accounts


# --------------------------------------------------------------------------
# link table
# --------------------------------------------------------------------------

def load_links():
    if LINKS_PATH.exists():
        return json.loads(LINKS_PATH.read_text(encoding="utf-8"))
    return {}


def save_links(links):
    """Write to a temp file, then swap it in, so a crash never leaves half a file."""
    LINKS_PATH.parent.mkdir(exist_ok=True)
    tmp = LINKS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(links, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, LINKS_PATH)


# --------------------------------------------------------------------------
# desired state
# --------------------------------------------------------------------------

def desired_record(worker, entra_user):
    """Built fresh from HR (and the Entra username) every time. Never cached."""
    first = worker["preferred_name"] or worker["first_name"]
    return {
        "login": upn_module.mail_nickname(entra_user["userPrincipalName"]),
        "full_name": f"{first} {worker['last_name']}",
        "dept": worker["department"],
    }


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

def diff(workers, owned, accounts, links):
    today = datetime.date.today().isoformat()
    ops, flags = [], []

    by_emp = {a["emp_no"]: a for a in accounts}
    by_login = {}
    for a in accounts:
        by_login.setdefault(a["login"].lower(), []).append(a)
    linked_emps = {emp: wid for wid, emp in links.items()}
    hr_ids = {w["worker_id"] for w in workers}

    for worker in sorted(workers, key=lambda w: w["worker_id"]):
        wid = worker["worker_id"]
        entra = owned.get(wid, [])
        if len(entra) > 1:
            flags.append(Op("FLAG", wid, "more than one Entra account carries "
                            "this workerId"))
            continue
        if not entra:
            continue  # no Entra account yet, so no username yet: wait
        entra_user = entra[0]
        want = desired_record(worker, entra_user)

        # Decision 3: HR grants, HR or Entra revokes.
        want_active = (should_be_enabled(worker, today)
                       and bool(entra_user.get("accountEnabled")))
        wants_account = should_have_account(worker)

        # ---- find this worker's Timeclock account ----
        account, adopted = None, False
        emp = links.get(wid)
        if emp is not None:
            account = by_emp.get(emp)
            if account is None:
                flags.append(Op("FLAG", wid, f"link points to emp_no {emp}, "
                                "which no longer exists in Timeclock"))
                continue
        else:
            candidates = by_login.get(want["login"].lower(), [])
            if len(candidates) == 1:
                cand = candidates[0]
                owner = linked_emps.get(cand["emp_no"])
                if owner and owner != wid:
                    flags.append(Op("FLAG", wid, f"login {want['login']} matches "
                                    f"emp_no {cand['emp_no']}, already linked "
                                    f"to {owner}"))
                    continue
                # Decision 4: exactly one match, nobody else's. Adopt.
                ops.append(Op("ADOPT", wid, f"re-link to emp_no "
                              f"{cand['emp_no']} by login {want['login']}",
                              {"emp_no": cand["emp_no"]}))
                account, adopted = cand, True
            elif len(candidates) > 1:
                flags.append(Op("FLAG", wid, f"{len(candidates)} accounts match "
                                f"login {want['login']}; not guessing"))
                continue

        # ---- no account ----
        if account is None:
            if wants_account and want_active:
                ops.append(Op("CREATE", wid, f"{want['login']}  ({want['dept']})",
                              {"record": want}))
            continue

        # ---- existing account ----
        emp_no = account["emp_no"]
        if wants_account:
            current = {k: account[k] for k in ("login", "full_name", "dept")}
            if current != want:
                changed = ", ".join(f"{k}: {current[k]!r} -> {want[k]!r}"
                                    for k in want if current[k] != want[k])
                ops.append(Op("REPLACE", wid, f"emp_no {emp_no}  {changed}",
                              {"emp_no": emp_no, "record": want}))
        if account["is_active"] and not want_active:
            reason = worker["status"] if not should_be_enabled(worker, today) \
                else "disabled in Entra"
            ops.append(Op("DEACTIVATE", wid, f"emp_no {emp_no} ({reason})",
                          {"emp_no": emp_no}))
        elif not account["is_active"] and want_active and wants_account:
            ops.append(Op("ACTIVATE", wid, f"emp_no {emp_no}",
                          {"emp_no": emp_no}))

    # ---- accounts nobody owns ----
    adopting = {op.payload["emp_no"] for op in ops if op.kind == "ADOPT"}
    for a in accounts:
        owner = linked_emps.get(a["emp_no"])
        if a["emp_no"] in adopting:
            continue
        if owner is None:
            ops.append(Op("ORPHAN", None, f"emp_no {a['emp_no']} login "
                          f"{a['login']} is linked to no worker"
                          f"{' and is ACTIVE' if a['is_active'] else ''}"))
        elif owner not in hr_ids:
            ops.append(Op("ORPHAN", owner, f"emp_no {a['emp_no']} linked to a "
                          "worker who is not in HR"))

    ops.sort(key=lambda o: (ORDER.get(o.kind, 5), o.worker_id or ""))
    return ops, flags


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def apply(ops, links, audit, crash_after_create=False):
    done = failed = 0
    for op in ops:
        if op.kind == "ORPHAN":
            audit.record(op="TC_ORPHAN", worker_id=op.worker_id,
                         status="reported", detail=op.detail)
            print(f"  ?? {op}")
            continue
        try:
            if op.kind == "ADOPT":
                links[op.worker_id] = op.payload["emp_no"]
                save_links(links)
            elif op.kind == "CREATE":
                created = tc_call("POST", "/api/employees",
                                  {**op.payload["record"], "is_active": True})
                if crash_after_create:
                    print(f"  ok {op}  -> emp_no {created['emp_no']}")
                    print("\n  !! simulated crash: link NOT saved. "
                          f"emp_no {created['emp_no']} is now an orphan.")
                    sys.exit(1)
                links[op.worker_id] = created["emp_no"]
                save_links(links)  # immediately, before anything else
                op.detail += f"  -> emp_no {created['emp_no']}"
            elif op.kind == "REPLACE":
                tc_call("PUT", f"/api/employees/{op.payload['emp_no']}",
                        op.payload["record"])
            elif op.kind == "DEACTIVATE":
                tc_call("POST", f"/api/employees/{op.payload['emp_no']}/deactivate")
            elif op.kind == "ACTIVATE":
                tc_call("POST", f"/api/employees/{op.payload['emp_no']}/activate")
            audit.record(op=f"TC_{op.kind}", worker_id=op.worker_id,
                         status="success", detail=op.detail)
            done += 1
            print(f"  ok {op}")
        except (TimeclockError, requests.RequestException) as exc:
            failed += 1
            audit.record(op=f"TC_{op.kind}", worker_id=op.worker_id,
                         status="failed", error=str(exc)[:400])
            print(f"  !! {op.kind} {op.worker_id} FAILED: {exc}")
    return done, failed


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--crash-after-create", action="store_true",
                        help="die after the first create, before saving its link")
    args = parser.parse_args()

    cfg = graph.config()
    correlation_id = str(uuid.uuid4())
    audit = Audit(correlation_id)

    workers = fetch_hr()["workers"]
    owned, _ = live_state(cfg["extension"])
    accounts = tc_read_all()
    links = load_links()

    ops, flags = diff(workers, owned, accounts, links)

    print(f"correlation id : {correlation_id}")
    print(f"mode           : {'APPLY' if args.apply else 'plan only'}")
    print(f"hr workers     : {len(workers)}")
    print(f"timeclock      : {len(accounts)} accounts, {len(links)} linked\n")

    if flags:
        print("FLAGS - need a human, not touched:\n")
        for f in flags:
            print(f"  !! {f}")
            audit.record(op="TC_FLAG", worker_id=f.worker_id,
                         status="flagged", detail=f.detail)
        print()

    if not ops:
        print("no changes. Timeclock matches HR.")
        return

    counts = {}
    for op in ops:
        counts[op.kind] = counts.get(op.kind, 0) + 1
    print("plan: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print()

    if not args.apply:
        for op in ops:
            print(f"  {op}")
        print(f"\n{len(ops)} operations. Nothing written - pass --apply to execute.")
        return

    print("applying:\n")
    done, failed = apply(ops, links, audit, args.crash_after_create)
    print(f"\n{done} succeeded, {failed} failed")


if __name__ == "__main__":
    main()
