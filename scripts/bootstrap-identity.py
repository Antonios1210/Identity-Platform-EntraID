#!/usr/bin/env python3
"""
bootstrap-identity.py — create the sync engine's identity in a tenant.

Step 1 of tenant rehydration. Runs as YOU (device code login), not as the
app, because the app doesn't exist yet. Idempotent: safe to re-run.

Creates or reuses:
  - the app registration
  - its service principal
  - Graph application permissions + admin consent
  - a client secret
  - the workerId directory extension on User

Writes .env with everything the engine needs, including the generated
extension name (which embeds the app ID and is different in every tenant).

Usage:
    python scripts/bootstrap-identity.py
    python scripts/bootstrap-identity.py --app-name identity-sync-engine
    python scripts/bootstrap-identity.py --rotate-secret
"""

import argparse
import datetime
import json
import pathlib
import sys
import time

import msal
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"

# Well-known pre-consented public client (Microsoft Graph PowerShell).
# Lets you sign in interactively without first registering an app.
BOOTSTRAP_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

LOGIN_SCOPES = [
    "Application.ReadWrite.All",
    "AppRoleAssignment.ReadWrite.All",
    "Directory.ReadWrite.All",
]

# Application permissions the engine needs. Resolved to GUIDs at runtime
# against the Graph service principal — never hardcoded.
REQUIRED_APP_ROLES = [
    "User.ReadWrite.All",
    "Group.ReadWrite.All",
    "GroupMember.ReadWrite.All",
    "Directory.Read.All",
    "Policy.Read.All",
    "EntitlementManagement.ReadWrite.All",
    "User-LifeCycleInfo.ReadWrite.All",
    "Synchronization.ReadWrite.All",
]

EXTENSION_NAME = "workerId"


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def login():
    """Device code flow. Prints a code; you paste it in a browser."""
    app = msal.PublicClientApplication(
        BOOTSTRAP_CLIENT_ID,
        authority="https://login.microsoftonline.com/organizations",
    )
    flow = app.initiate_device_flow(scopes=LOGIN_SCOPES)
    if "user_code" not in flow:
        sys.exit(f"could not start device flow: {json.dumps(flow, indent=2)}")
    print(f"\n{flow['message']}\n")
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        sys.exit(f"login failed: {result.get('error_description', '')[:300]}")
    return result["access_token"]


def call(method, path, token, body=None, ok=(200, 201, 204)):
    url = path if path.startswith("http") else f"{GRAPH}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "ConsistencyLevel": "eventual",
    }
    for attempt in range(5):
        resp = requests.request(
            method, url, headers=headers, json=body, timeout=60
        )
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            print(f"    429 — sleeping {wait}s")
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


def first(items):
    return items[0] if items else None


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def tenant_info(token):
    org = call("GET", "/organization", token)["value"][0]
    verified = [d for d in org["verifiedDomains"] if d.get("isDefault")]
    return {
        "tenant_id": org["id"],
        "tenant_name": org["displayName"],
        "default_domain": verified[0]["name"] if verified else None,
    }


def graph_sp(token):
    """The Microsoft Graph service principal — source of appRole GUIDs."""
    sp = first(
        call(
            "GET",
            f"/servicePrincipals?$filter=appId eq '{GRAPH_APP_ID}'"
            "&$select=id,appId,appRoles",
            token,
        )["value"]
    )
    if not sp:
        sys.exit("Microsoft Graph service principal not found in tenant")
    return sp


def resolve_app_roles(sp, wanted):
    """Map permission names to appRole GUIDs. Warns on anything unknown."""
    by_value = {
        role["value"]: role["id"]
        for role in sp["appRoles"]
        if "Application" in role.get("allowedMemberTypes", [])
    }
    resolved, missing = {}, []
    for name in wanted:
        if name in by_value:
            resolved[name] = by_value[name]
        else:
            missing.append(name)
    if missing:
        print(f"[!] not available in this tenant, skipping: {', '.join(missing)}")
    return resolved


def ensure_application(token, app_name, graph_sp_id, app_roles):
    existing = first(
        call(
            "GET",
            f"/applications?$filter=displayName eq '{app_name}'"
            "&$select=id,appId,displayName",
            token,
        )["value"]
    )
    required = {
        "resourceAppId": GRAPH_APP_ID,
        "resourceAccess": [
            {"id": role_id, "type": "Role"} for role_id in app_roles.values()
        ],
    }

    if existing:
        print(f"[=] app registration exists: {app_name}")
        call(
            "PATCH",
            f"/applications/{existing['id']}",
            token,
            {"requiredResourceAccess": [required]},
        )
        return existing

    print(f"[+] creating app registration: {app_name}")
    app = call(
        "POST",
        "/applications",
        token,
        {
            "displayName": app_name,
            "signInAudience": "AzureADMyOrg",
            "requiredResourceAccess": [required],
        },
    )
    time.sleep(10)  # directory replication
    return app


def ensure_service_principal(token, app_id):
    existing = first(
        call(
            "GET",
            f"/servicePrincipals?$filter=appId eq '{app_id}'&$select=id,appId",
            token,
        )["value"]
    )
    if existing:
        print("[=] service principal exists")
        return existing
    print("[+] creating service principal")
    sp = call("POST", "/servicePrincipals", token, {"appId": app_id})
    time.sleep(10)
    return sp


def grant_consent(token, app_sp_id, graph_sp_id, app_roles):
    """Admin consent = an appRoleAssignment per permission. No portal click."""
    current = call(
        "GET", f"/servicePrincipals/{app_sp_id}/appRoleAssignments", token
    )["value"]
    already = {a["appRoleId"] for a in current}

    for name, role_id in app_roles.items():
        if role_id in already:
            print(f"    [=] {name}")
            continue
        call(
            "POST",
            f"/servicePrincipals/{app_sp_id}/appRoleAssignedTo",
            token,
            {
                "principalId": app_sp_id,
                "resourceId": graph_sp_id,
                "appRoleId": role_id,
            },
        )
        print(f"    [+] {name}")


def ensure_extension(token, app_object_id):
    existing = call(
        "GET", f"/applications/{app_object_id}/extensionProperties", token
    )["value"]
    for ext in existing:
        if ext["name"].endswith(f"_{EXTENSION_NAME}"):
            print(f"[=] extension exists: {ext['name']}")
            return ext["name"]

    print(f"[+] creating directory extension: {EXTENSION_NAME}")
    ext = call(
        "POST",
        f"/applications/{app_object_id}/extensionProperties",
        token,
        {
            "name": EXTENSION_NAME,
            "dataType": "String",
            "targetObjects": ["User"],
        },
    )
    return ext["name"]


def create_secret(token, app_object_id, years=1):
    expiry = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=365 * years
    )
    result = call(
        "POST",
        f"/applications/{app_object_id}/addPassword",
        token,
        {
            "passwordCredential": {
                "displayName": "bootstrap",
                "endDateTime": expiry.isoformat(),
            }
        },
    )
    return result["secretText"], result["endDateTime"]


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-name", default="identity-sync-engine")
    parser.add_argument(
        "--rotate-secret",
        action="store_true",
        help="mint a new secret even if .env already has one",
    )
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    env_path = pathlib.Path(args.env_file)
    if env_path.exists() and not args.rotate_secret:
        sys.exit(
            f"{env_path} already exists. Delete it, or pass --rotate-secret "
            "to mint a new secret into a fresh file."
        )

    token = login()

    info = tenant_info(token)
    print(f"\ntenant : {info['tenant_name']}")
    print(f"id     : {info['tenant_id']}")
    print(f"domain : {info['default_domain']}\n")

    gsp = graph_sp(token)
    app_roles = resolve_app_roles(gsp, REQUIRED_APP_ROLES)

    app = ensure_application(token, args.app_name, gsp["id"], app_roles)
    sp = ensure_service_principal(token, app["appId"])

    print("[*] granting admin consent")
    grant_consent(token, sp["id"], gsp["id"], app_roles)

    ext_name = ensure_extension(token, app["id"])

    print("[+] creating client secret")
    secret, secret_expiry = create_secret(token, app["id"])

    env_path.write_text(
        "\n".join(
            [
                "# generated by bootstrap-identity.py — do not commit",
                f"# tenant: {info['tenant_name']}",
                f"# secret expires: {secret_expiry}",
                f"TENANT_ID={info['tenant_id']}",
                f"CLIENT_ID={app['appId']}",
                f"CLIENT_SECRET={secret}",
                f"UPN_SUFFIX={info['default_domain']}",
                f"WORKER_ID_EXTENSION={ext_name}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(f"\nwrote {env_path}")
    print(f"extension: {ext_name}")
    print(
        "\nConsent can take a few minutes to propagate. If the first engine "
        "run returns 403, wait and retry before debugging anything."
    )


if __name__ == "__main__":
    main()
