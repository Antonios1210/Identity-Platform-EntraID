"""
graph.py — the one place that knows how to talk to Microsoft Graph.

Auth, retry, and pagination live here so the rest of the engine can
just ask for data. This is the code that was copy-pasted across the
bootstrap and measurement scripts; from here on it has one home.
"""

import os
import pathlib
import sys
import time

import msal
import requests
from dotenv import load_dotenv

GRAPH = "https://graph.microsoft.com/v1.0"
ROOT = pathlib.Path(__file__).parent.parent

_token_cache = {"value": None, "expires_at": 0.0}


def config():
    """Everything tenant-specific comes from .env. Nothing is hardcoded."""
    load_dotenv(ROOT / ".env")
    missing = [
        key for key in
        ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET",
         "UPN_SUFFIX", "WORKER_ID_EXTENSION")
        if not os.environ.get(key)
    ]
    if missing:
        sys.exit(f"missing from .env: {', '.join(missing)}")
    return {
        "tenant_id": os.environ["TENANT_ID"],
        "client_id": os.environ["CLIENT_ID"],
        "client_secret": os.environ["CLIENT_SECRET"],
        "upn_suffix": os.environ["UPN_SUFFIX"],
        "extension": os.environ["WORKER_ID_EXTENSION"],
    }


def token():
    """Cached client-credentials token. Refreshed 2 minutes before expiry."""
    if _token_cache["value"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["value"]

    cfg = config()
    app = msal.ConfidentialClientApplication(
        cfg["client_id"],
        authority=f"https://login.microsoftonline.com/{cfg['tenant_id']}",
        client_credential=cfg["client_secret"],
    )
    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        sys.exit(f"auth failed: {result.get('error_description', '')[:300]}")

    _token_cache["value"] = result["access_token"]
    _token_cache["expires_at"] = time.time() + result.get("expires_in", 3600) - 120
    return _token_cache["value"]


class GraphError(RuntimeError):
    def __init__(self, method, url, status, body):
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> {status}\n{body[:500]}")


def call(method, path, body=None, ok=(200, 201, 202, 204), retries=5):
    """
    One Graph request. Handles 429 with Retry-After and 5xx with
    exponential backoff. Raises GraphError on anything else so callers
    can inspect .status.
    """
    url = path if path.startswith("http") else f"{GRAPH}{path}"
    headers = {
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
        "ConsistencyLevel": "eventual",
    }
    for attempt in range(retries):
        resp = requests.request(method, url, headers=headers,
                                json=body, timeout=60)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt))
            time.sleep(wait)
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        if resp.status_code not in ok:
            raise GraphError(method, url, resp.status_code, resp.text)
        if resp.status_code == 204 or not resp.text:
            return None
        return resp.json()
    raise GraphError(method, url, resp.status_code, resp.text)


def collect(path):
    """
    GET every page. Following @odata.nextLink is not optional - miss it
    and you silently process only the first 100 objects.
    """
    items, url = [], path
    while url:
        page = call("GET", url)
        items.extend(page.get("value", []))
        url = page.get("@odata.nextLink")
    return items
