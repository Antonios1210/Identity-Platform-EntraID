#!/usr/bin/env python3
"""
export-tenant.py — snapshot Entra ID tenant configuration to JSON.

Run this every session. The exports/ directory is the only thing that
survives the lab tenant expiring.

Usage:
    python scripts/export-tenant.py
    python scripts/export-tenant.py --out exports/pre-cleanup
"""

import argparse
import datetime
import json
import os
import pathlib
import sys
import time

import msal
import requests
from dotenv import load_dotenv

GRAPH = "https://graph.microsoft.com/v1.0"

# Resources to snapshot. Add to this list as you build out later phases.
RESOURCES = {
    "users": (
        f"{GRAPH}/users"
        "?$select=id,userPrincipalName,displayName,givenName,surname,mail,"
        "accountEnabled,employeeId,employeeType,employeeHireDate,department,"
        "jobTitle,companyName,usageLocation,createdDateTime,onPremisesSyncEnabled"
        "&$top=999"
    ),
    "groups": (
        f"{GRAPH}/groups"
        "?$select=id,displayName,description,groupTypes,membershipRule,"
        "membershipRuleProcessingState,mailEnabled,securityEnabled,"
        "assignedLicenses,createdDateTime"
        "&$top=999"
    ),
    "applications": f"{GRAPH}/applications?$top=999",
    "servicePrincipals": f"{GRAPH}/servicePrincipals?$top=999",
    "conditionalAccessPolicies": f"{GRAPH}/identity/conditionalAccess/policies",
    "namedLocations": f"{GRAPH}/identity/conditionalAccess/namedLocations",
    "authenticationMethodsPolicy": f"{GRAPH}/policies/authenticationMethodsPolicy",
    "administrativeUnits": f"{GRAPH}/directory/administrativeUnits",
    "directoryRoles": f"{GRAPH}/directoryRoles",
    "subscribedSkus": f"{GRAPH}/subscribedSkus",
    "emCatalogs": f"{GRAPH}/identityGovernance/entitlementManagement/catalogs",
    "emAccessPackages": f"{GRAPH}/identityGovernance/entitlementManagement/accessPackages",
    "emAssignmentPolicies": f"{GRAPH}/identityGovernance/entitlementManagement/assignmentPolicies",
    "accessReviewDefinitions": f"{GRAPH}/identityGovernance/accessReviews/definitions",
}

MAX_RETRIES = 5


def get_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    app = msal.ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        sys.exit(
            f"auth failed: {result.get('error')} — "
            f"{result.get('error_description', '')[:300]}"
        )
    return result["access_token"]


def graph_get(url: str, token: str):
    """GET with 429 backoff. Returns the parsed body, or raises."""
    headers = {
        "Authorization": f"Bearer {token}",
        "ConsistencyLevel": "eventual",
    }
    for attempt in range(MAX_RETRIES):
        resp = requests.get(url, headers=headers, timeout=60)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"    429 — sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"gave up after {MAX_RETRIES} attempts: {url}")


def collect(url: str, token: str):
    """Follow @odata.nextLink until exhausted. Single objects pass through."""
    items = []
    while url:
        body = graph_get(url, token)
        if "value" not in body:
            return body  # singleton resource, e.g. authenticationMethodsPolicy
        items.extend(body["value"])
        url = body.get("@odata.nextLink")
    return items


def expand_group_members(groups, token):
    """Membership is the control plane — a group export without members is useless."""
    for group in groups:
        gid = group["id"]
        try:
            members = collect(
                f"{GRAPH}/groups/{gid}/members"
                "?$select=id,userPrincipalName,displayName&$top=999",
                token,
            )
            owners = collect(
                f"{GRAPH}/groups/{gid}/owners?$select=id,userPrincipalName&$top=999",
                token,
            )
            group["_members"] = members
            group["_owners"] = owners
        except Exception as exc:  # noqa: BLE001
            group["_members_error"] = str(exc)[:200]
    return groups


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        default=None,
        help="output directory (default: exports/<UTC date>)",
    )
    args = parser.parse_args()

    load_dotenv()
    try:
        tenant_id = os.environ["TENANT_ID"]
        client_id = os.environ["CLIENT_ID"]
        client_secret = os.environ["CLIENT_SECRET"]
    except KeyError as missing:
        sys.exit(f"missing env var {missing} — check your .env file")

    stamp = datetime.datetime.now(datetime.timezone.utc)
    out_dir = pathlib.Path(
        args.out or f"exports/{stamp.strftime('%Y-%m-%d')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    token = get_token(tenant_id, client_id, client_secret)

    manifest = {
        "exported_at": stamp.isoformat(),
        "tenant_id": tenant_id,
        "resources": {},
    }

    for name, url in RESOURCES.items():
        print(f"[*] {name}")
        try:
            data = collect(url, token)
        except requests.HTTPError as exc:
            status = exc.response.status_code
            detail = exc.response.text[:300]
            print(f"    {status} — skipped", file=sys.stderr)
            manifest["resources"][name] = {
                "status": "error",
                "http_status": status,
                "detail": detail,
            }
            continue

        if name == "groups" and isinstance(data, list):
            data = expand_group_members(data, token)

        count = len(data) if isinstance(data, list) else 1
        (out_dir / f"{name}.json").write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8"
        )
        manifest["resources"][name] = {"status": "ok", "count": count}
        print(f"    {count}")

    (out_dir / "_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {out_dir}")


if __name__ == "__main__":
    main()
