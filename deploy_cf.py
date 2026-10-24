"""Deploy the balance check to Cloudflare Workers via the REST API.

The local machine cannot run the Cloudflare toolchain (its macOS is too old for
workerd/esbuild), so this script packages the Python modules itself and PUTs
them to the Workers Scripts API (ES module format). Local code stays the source
of truth; the worker opens a Durable Object per account name to remember the
previous check outcome, which is what keeps "recovered" from repeating.

Usage:
    CF_API_TOKEN=... CF_ACCOUNT_ID=... CHECK_TOKEN=... ACCOUNTS_JSON=... \
        python3 deploy_cf.py [--name balance-check] [--api-url ...]

The token needs "Workers Scripts: Edit". Secrets are sent as plaintext bindings
and are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMPATIBILITY_DATE = "2025-06-01"


def build_bundle(name: str) -> dict:
    """Ask wrangler for the Python module bundle without uploading anything."""
    modules = [{
        "name": path.name,
        "content": path.read_text(encoding="utf-8"),
        "type": "text",
    } for path in sorted(ROOT.glob("*.py")) if path.name not in ("deploy_cf.py",)]
    # `main_module` the last: Cloudflare uploads the first entry as the entry point
    # only when it is listed first, so keep worker_app.py at the head.
    modules.sort(key=lambda m: (m["name"] != "worker_app.py", m["name"]))
    return {"main_module": "worker_app.py", "modules": modules}


def upload(name: str, account_id: str, token: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{name}",
        data=body,
        method="PUT",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"upload failed: HTTP {exc.code}: {detail[:2000]}") from None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=os.environ.get("WORKER_NAME", "balance-check"))
    parser.add_argument("--account-id", default=os.environ.get("CF_ACCOUNT_ID", ""))
    parser.add_argument("--api-url", default=os.environ.get("BALANCE_API_URL", ""))
    parser.add_argument("--dry-run", action="store_true", help="print the module list only")
    args = parser.parse_args()

    accounts = os.environ.get("ACCOUNTS_JSON", "").strip()
    if not accounts:
        raise SystemExit("ACCOUNTS_JSON is required")
    json.loads(accounts)  # fail early on malformed JSON, without echoing it

    bundle = build_bundle(args.name)
    payload = {
        **bundle,
        "compatibility_date": COMPATIBILITY_DATE,
        # Python Workers still need the flag while the runtime is beta.
        "compatibility_flags": ["python_workers"],
        "observability": {"enabled": True},
        "bindings": [
            # One Durable Object per account double-checks "recovered" transitions.
            {"type": "durable_object_namespace", "name": "ACCOUNT_STATE", "class_name": "AccountState"},
            {"type": "plain_text", "name": "CHECK_TOKEN", "text": os.environ["CHECK_TOKEN"]},
            {"type": "plain_text", "name": "ACCOUNTS_JSON", "text": accounts},
            {"type": "plain_text", "name": "BALANCE_API_URL", "text": args.api_url},
        ],
    }
    if args.dry_run:
        print(json.dumps({"main_module": bundle["main_module"], "modules": [m["name"] for m in bundle["modules"]]}, indent=2))
        return 0

    token = os.environ.get("CF_API_TOKEN", "").strip()
    if not token or not args.account_id:
        raise SystemExit("CF_API_TOKEN and CF_ACCOUNT_ID are required")
    result = upload(args.name, args.account_id, token, payload)
    if not result.get("success"):
        raise SystemExit(f"upload failed: {json.dumps(result.get('errors'))[:2000]}")
    print(f"deployed {args.name} revision {result['result'].get('id', '?')[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
