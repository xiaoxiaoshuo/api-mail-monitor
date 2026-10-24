"""Cloudflare Python Worker entry point (ES module) for balance_check.

`deploy_cf.py` uploads this module as `worker.py`; wrangler builds the same
entry when deploying from a checkout. Each account gets a Durable Object that
stores its last outcome, so `/check/<account>` can report both the current
numbers and a one-shot `recovered` flag to yunji-task.
"""
from js import Response

from balance_check import (
    ConfigError,
    Account,
    api_url,
    check_account,
    parse_accounts,
)
import json
from urllib.parse import urlsplit

CORS = {
    "cache-control": "no-store",
    "content-type": "application/json; charset=utf-8",
    "access-control-allow-origin": "*",
}


def _json(status, payload):
    return Response.new(
        json.dumps(payload, ensure_ascii=False),
        {"status": status, "headers": CORS},
    )


def _error(status, code):
    return _json(status, {"error": code})


def _accounts(env):
    try:
        return parse_accounts(json.loads(env.ACCOUNTS_JSON or "null")), api_url(env.BALANCE_API_URL)
    except (ConfigError, ValueError, AttributeError):
        return None, None


class AccountState:
    """Per-account outcome, one Durable Object each."""

    def __init__(self, ctx, env):
        self.ctx = ctx
        self.env = env
        ctx.blockConcurrencyWhile(self._load)

    async def _load(self):
        stored = await self.ctx.storage.get("outcome")
        self.outcome = stored if stored in ("ok", "failed") else None

    async def fetch(self, request):
        # Called by `fetch` over a same-worker stub with a serialized, computed result.
        data = json.loads(await request.text())
        status = int(data["status"])
        payload = data["payload"]
        outcome = "ok" if status == 200 else "failed"
        payload["recovered"] = bool(status == 200 and self.outcome == "failed")
        if self.outcome != outcome:
            await self.ctx.storage.put("outcome", outcome)
            self.outcome = outcome
        return Response.new(
            json.dumps(payload, ensure_ascii=False),
            {"status": status, "headers": CORS},
        )


async def fetch(request, env, ctx):
    path = urlsplit(str(request.url)).path.rstrip("/") or "/"
    if path in ("", "/", "/health"):
        return _json(200, {"ok": True, "service": "balance-check"})
    if not path.startswith("/check/"):
        return _error(404, "not_found")
    if request.method != "GET":
        return _error(405, "method_not_allowed")

    token = (env.CHECK_TOKEN or "").strip()
    if not token:
        return _error(503, "CHECK_TOKEN is not configured")
    if (request.headers.get("authorization") or "").strip() != f"Bearer {token}":
        return _error(401, "unauthorized")

    accounts, url = _accounts(env)
    if accounts is None:
        return _error(500, "accounts configuration invalid")

    name = path[len("/check/"):]
    account = next((a for a in accounts if a.name == name), None)
    if account is None:
        return _json(404, {"error": "unknown account", "accounts": [a.name for a in accounts]})

    result = check_account(account, url)
    status = 200 if result.ok else 503
    payload = {
        **result.as_json(),
        "rule": f"{account.value_path} {account.operator} {account.threshold}",
    }
    stub = env.ACCOUNT_STATE.get(env.ACCOUNT_STATE.idFromName(account.name))
    return await stub.fetch(
        "https://state.internal/",
        {"method": "POST", "body": json.dumps({"status": status, "payload": payload}, ensure_ascii=False)},
    )


default = fetch
