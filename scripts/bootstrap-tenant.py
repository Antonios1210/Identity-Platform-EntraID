#!/usr/bin/env python3
"""
bootstrap-tenant.py — build the engine's Entra objects from config.

Step 2 of tenant rehydration. Runs as the APP (client credentials from
.env), unlike bootstrap-identity.py which runs as you.

Idempotent: creates what's missing, patches what has drifted, leaves
the rest alone. Safe to run every session.

Creates from config/tenant.json:
  - one dynamic security group per department
  - one assigned birthright group
  - group-based licensing on the designated department group

Resolved GUIDs land in state/tenant-objects.json (gitignored — they
differ in every tenant).

Usage:
    python scripts/bootstrap-tenant.py
    python scripts/bootstrap-tenant.py --whatif
    python scripts/bootstrap-tenant.py --list-skus
"""

import argparse
import json
import os
import pathlib
import sys
import time

import msal
import requests
from dotenv import load_dotenv

GRAPH = "https://graph.microsoft.com/v1.0"
ROOT = pathlib.Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config" / "tenant.json"
STATE_PATH = ROOT / "state" / "tenant-objects.json"


def get_token():
    load_dotenv(ROOT / ".env")
    try:
        tenant = os.environ["TENANT_ID"]
        client = os.environ["CLIENT_ID"]
        secret = os.environ["CLIENT_SECRET"]
    except KeyError as missing:
        sys.exit(f"missing {missing} in .env — run bootstrap-identity.py first")

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


def call(method, path, token, body=None, ok=(200, 201, 202, 204)):
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
            print(f"    429 — sleeping {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code not in ok:
            raise RuntimeError(
                f"{method} {url} -> {resp.status_code}\n{resp.text[:600]}"
            )
        if resp.status_code == 204 or not resp.text:
            return None
        return resp.json()
    raise RuntimeError(f"gave up after retries: {method} {url}")


def collect(path, token):
    items, url = [], path
    while url:
        body = call("GET", url, token)
        items.extend(body.get("value", []))
        url = body.get("@odata.nextLink")
    return items


def mail_nickname(display_name):
    return "".join(c for c in display_name if c.isalnum() or c == "-").lower()


def department_rule(department, extension):
    """
    The workerId clause is load-bearing. Drop it and this rule matches
    every prebuilt user in the tenant who happens to share a department.
    """
    return (
        f'(user.department -eq "{department}") '
        f'and (user.{extension} -ne null)'
    )


def ensure_group(token, display_name, description, rule, whatif):
    existing = call(
        "GET",
        f"/groups?$filter=displayName eq '{display_name}'"
        "&$select=id,displayName,membershipRule,membershipRuleProcessingState",
        token,
    )["value"]

    if existing:
        group = existing[0]
        if rule and group.get("membershipRule") != rule:
            print(f"    [~] {display_name}: rule drifted")
            if whatif:
                print(f"        would set: {rule}")
            else:
                call("PATCH", f"/groups/{group['id']}", token,
                     {"membershipRule": rule,
                      "membershipRuleProcessingState": "On"})
                print("        rule updated")
        else:
            print(f"    [=] {display_name}")
        return group["id"]

    if whatif:
        print(f"    [+] would create {display_name}")
        return None

    body = {
        "displayName": display_name,
        "description": description,
        "mailEnabled": False,
        "mailNickname": mail_nickname(display_name),
        "securityEnabled": True,
    }
    if rule:
        body["groupTypes"] = ["DynamicMembership"]
        body["membershipRule"] = rule
        body["membershipRuleProcessingState"] = "On"

    group = call("POST", "/groups", token, body)
    print(f"    [+] {display_name}")
    return group["id"]


def resolve_sku(token, part_number):
    skus = collect("/subscribedSkus", token)
    for sku in skus:
        if sku["skuPartNumber"].upper() == part_number.upper():
            available = (
                sku["prepaidUnits"]["enabled"] - sku["consumedUnits"]
            )
            return sku["skuId"], available
    print(f"\n[!] SKU '{part_number}' not found. Available in this tenant:")
    for sku in skus:
        units = sku["prepaidUnits"]["enabled"]
        used = sku["consumedUnits"]
        print(f"      {sku['skuPartNumber']:<40} {used}/{units} used")
    sys.exit(1)


def assign_license_to_group(token, group_id, sku_id, whatif):
    current = call(
        "GET", f"/groups/{group_id}?$select=assignedLicenses", token
    ).get("assignedLicenses", [])

    if any(lic["skuId"] == sku_id for lic in current):
        print("    [=] license already assigned to group")
        return

    if whatif:
        print("    [+] would assign license to group")
        return

    call(
        "POST",
        f"/groups/{group_id}/assignLicense",
        token,
        {
            "addLicenses": [{"disabledPlans": [], "skuId": sku_id}],
            "removeLicenses": [],
        },
    )
    print("    [+] license assigned to group")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--whatif", action="store_true",
                        help="print planned changes without making them")
    parser.add_argument("--list-skus", action="store_true",
                        help="list the tenant's SKUs and exit")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    extension = os.environ.get("WORKER_ID_EXTENSION")
    if not extension:
        sys.exit("WORKER_ID_EXTENSION missing from .env")

    token = get_token()

    if args.list_skus:
        for sku in collect("/subscribedSkus", token):
            units = sku["prepaidUnits"]["enabled"]
            print(f"{sku['skuPartNumber']:<45} "
                  f"{sku['consumedUnits']}/{units} used   {sku['skuId']}")
        return

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    prefix = config["prefix"]
    state = {"extension": extension, "groups": {}}

    if args.whatif:
        print("--whatif: nothing will be changed\n")

    print("[*] department groups (dynamic)")
    for dept in config["departments"]:
        name = f"{prefix}-Dept-{dept}"
        rule = department_rule(dept, extension)
        gid = ensure_group(
            token, name,
            f"Workers in {dept}, managed by the sync engine.",
            rule, args.whatif,
        )
        if gid:
            state["groups"][name] = gid

    print("\n[*] birthright group (assigned)")
    br = config["birthright_group"]
    gid = ensure_group(token, br["displayName"], br["description"],
                       None, args.whatif)
    if gid:
        state["groups"][br["displayName"]] = gid
        state["birthright_group_id"] = gid

    print("\n[*] group-based licensing")
    sku_id, available = resolve_sku(token, config["license_sku_part_number"])
    print(f"    SKU {config['license_sku_part_number']}: "
          f"{available} seats free")
    target = f"{prefix}-Dept-{config['licensed_department']}"
    target_id = state["groups"].get(target)
    if target_id:
        assign_license_to_group(token, target_id, sku_id, args.whatif)
        state["license_sku_id"] = sku_id
        state["licensed_group_id"] = target_id
    else:
        print(f"    [!] {target} not created yet — rerun without --whatif")

    if not args.whatif:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
        print(f"\nwrote {STATE_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
