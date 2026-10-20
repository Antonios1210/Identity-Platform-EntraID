#!/usr/bin/env python3
"""
measure-latency.py — put a real number on two things everyone guesses at.

Creates one disposable user with department=Engineering and a workerId,
then polls until:
  1. the dynamic group rule picks them up
  2. group-based licensing lands a license on them

Both numbers go into docs/measurements.md. Deletes the user afterwards.

Usage:
    python scripts/measure-latency.py
    python scripts/measure-latency.py --keep        # don't delete the user
    python scripts/measure-latency.py --timeout 45  # minutes
    python scripts/measure-latency.py --cleanup     # delete leftovers and exit
"""

import argparse
import datetime
import json
import os
import pathlib
import secrets
import string
import sys
import time

import msal
import requests
from dotenv import load_dotenv

GRAPH = "https://graph.microsoft.com/v1.0"
ROOT = pathlib.Path(__file__).parent.parent
STATE_PATH = ROOT / "state" / "tenant-objects.json"
MEASUREMENTS = ROOT / "docs" / "measurements.md"

TEST_PREFIX = "idp-latency-probe"
POLL_SECONDS = 10


def get_token():
    load_dotenv(ROOT / ".env")
    try:
        tenant = os.environ["TENANT_ID"]
        client = os.environ["CLIENT_ID"]
        secret = os.environ["CLIENT_SECRET"]
    except KeyError as missing:
        sys.exit(f"missing {missing} in .env")

    app = msal.ConfidentialClientApplication(
        client,
        authority=f"https://login.microsoftonline.com/{tenant}",
        client_credential=secret,
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        sys.exit(f"auth failed: {result.get('error_description', '')[:300]}")
    return result["access_token"]


def call(method, path, token, body=None, ok=(200, 201, 204)):
    url = path if path.startswith("http") else f"{GRAPH}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "ConsistencyLevel": "eventual",
    }
    for attempt in range(5):
        resp = requests.request(method, url, headers=headers,
                                json=body, timeout=60)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            time.sleep(wait)
            continue
        if resp.status_code not in ok:
            raise RuntimeError(
                f"{method} {url} -> {resp.status_code}\n{resp.text[:500]}"
            )
        if resp.status_code == 204 or not resp.text:
            return None
        return resp.json()
    raise RuntimeError(f"gave up after retries: {method} {url}")


def strong_password():
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(24))


def find_probes(token, upn_suffix):
    return call(
        "GET",
        f"/users?$filter=startswith(userPrincipalName,'{TEST_PREFIX}')"
        "&$select=id,userPrincipalName,createdDateTime",
        token,
    )["value"]


def delete_user(token, user_id, upn):
    call("DELETE", f"/users/{user_id}", token, ok=(204,))
    print(f"[-] deleted {upn}")


def elapsed(start):
    return time.monotonic() - start


def fmt(seconds):
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f}min"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true",
                        help="leave the probe user in place")
    parser.add_argument("--timeout", type=int, default=60,
                        help="minutes to wait before giving up (default 60)")
    parser.add_argument("--cleanup", action="store_true",
                        help="delete any leftover probe users and exit")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    upn_suffix = os.environ["UPN_SUFFIX"]
    extension = os.environ["WORKER_ID_EXTENSION"]

    token = get_token()

    if args.cleanup:
        probes = find_probes(token, upn_suffix)
        if not probes:
            print("no probe users found")
            return
        for user in probes:
            delete_user(token, user["id"], user["userPrincipalName"])
        return

    if not STATE_PATH.exists():
        sys.exit("state/tenant-objects.json missing — run bootstrap-tenant.py")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    group_id = state["licensed_group_id"]
    sku_id = state["license_sku_id"]

    group = call("GET", f"/groups/{group_id}?$select=displayName", token)
    print(f"target group : {group['displayName']}")
    print(f"target sku   : {sku_id}\n")

    stamp = datetime.datetime.now(datetime.timezone.utc)
    upn = f"{TEST_PREFIX}-{stamp.strftime('%H%M%S')}@{upn_suffix}"

    body = {
        "accountEnabled": True,
        "displayName": "IDP Latency Probe",
        "mailNickname": TEST_PREFIX,
        "userPrincipalName": upn,
        "department": "Engineering",
        "jobTitle": "Latency Probe",
        "usageLocation": "US",
        "passwordProfile": {
            "forceChangePasswordNextSignIn": True,
            "password": strong_password(),
        },
        extension: f"PROBE-{stamp.strftime('%Y%m%d%H%M%S')}",
    }

    t0 = time.monotonic()
    user = call("POST", "/users", token, body)
    user_id = user["id"]
    print(f"[+] created {upn}")
    print(f"    department=Engineering, usageLocation=US, "
          f"{extension.split('_')[-1]} set\n")

    deadline = t0 + args.timeout * 60
    group_at = None
    license_at = None

    print("polling every 10s...\n")
    while time.monotonic() < deadline:
        if group_at is None:
            members = call(
                "GET", f"/users/{user_id}/memberOf?$select=id", token
            )["value"]
            if any(m["id"] == group_id for m in members):
                group_at = elapsed(t0)
                print(f"[*] dynamic group membership   {fmt(group_at)}")

        if group_at is not None and license_at is None:
            details = call(
                "GET", f"/users/{user_id}/licenseDetails", token
            )["value"]
            if any(d["skuId"] == sku_id for d in details):
                license_at = elapsed(t0)
                print(f"[*] license assigned           {fmt(license_at)}")

        if group_at is not None and license_at is not None:
            break

        time.sleep(POLL_SECONDS)

    print()
    if group_at is None:
        print(f"[!] no group membership after {args.timeout}min")
    if license_at is None:
        print(f"[!] no license after {args.timeout}min")

    lines = [
        "",
        f"## {stamp.strftime('%Y-%m-%d %H:%M UTC')} - dynamic membership "
        "and licensing latency",
        "",
        f"Probe user created with department=Engineering, usageLocation=US,",
        f"and the workerId extension set. Target group "
        f"{group['displayName']}.",
        "",
        "| Event | Elapsed from user creation |",
        "|---|---|",
        f"| Dynamic group membership evaluated | "
        f"{fmt(group_at) if group_at else f'not within {args.timeout}min'} |",
        f"| Group-based license assigned | "
        f"{fmt(license_at) if license_at else f'not within {args.timeout}min'} |",
        "",
    ]
    if group_at and license_at:
        lines.append(
            f"Licensing lagged membership by {fmt(license_at - group_at)}."
        )
        lines.append("")

    with MEASUREMENTS.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print(f"appended to {MEASUREMENTS.relative_to(ROOT)}")

    if args.keep:
        print(f"\nprobe user left in place: {upn}")
        print("remove it later with --cleanup")
    else:
        delete_user(token, user_id, upn)


if __name__ == "__main__":
    main()
