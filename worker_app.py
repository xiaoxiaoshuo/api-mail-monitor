"""Cloudflare Worker entry: one account check per request.

Deploy with `wrangler deploy`. Every account task in yunji-task calls
`GET /check/<account>` with `Authorization: Bearer <CHECK_TOKEN>`; accounts come
from the `ACCOUNTS_JSON` secret:

    [{"name":"账号A","key":"mk-...","threshold":1500}]

Optional per account: `operator` (lt/lte/gte/gt/eq/ne/between), `thresholdTo`
and `path` (JSON path of the numeric field, default `data.balance`).

The response is JSON `{account, ok, value, threshold, error}`; healthy checks
answer 200, failures answer 503 so the platform's retry/alert path runs. Amounts
are strings, so the platform's alert template renders them unchanged.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from balance_check import (
    ConfigError,
    api_url,
    check_account,
    parse_accounts,
)

CORS = {
    "cache-control": "no-store",
    "content-type": "application/json; charset=utf-8",
}


def _json(status: int, payload: Mapping[str, Any]) -> tuple[int, dict[str, str], str]:
    return status, CORS, json.dumps(payload, ensure_ascii=False)


def handler(
    request: Any,
    env: Mapping[str, str],
    previous: str | None = None,
    record: Any = None,
) -> tuple[int, dict[str, str], str]:
    """Pure request handler so it can be tested without the Workers runtime.

    `previous`/`record` are the account's last stored outcome; the Workers
    entry point supplies them from a Durable Object. On a 200 the body carries
    `"recovered": true` exactly once after a failure.
    """
    method = getattr(request, "method", "GET")
    path = "/" + getattr(request, "pathname", "/").strip("/")
    if method != "GET":
        return _json(405, {"error": "method_not_allowed"})
    if path in ("/", "/health"):
        return _json(200, {"ok": True, "service": "balance-check"})
    if not path.startswith("/check/"):
        return _json(404, {"error": "not_found"})

    token = (getattr(env, "CHECK_TOKEN", "") or "").strip()
    authorization = (getattr(request, "authorization", "") or "").strip()
    if not token:
        return _json(503, {"error": "CHECK_TOKEN is not configured"})
    if authorization != f"Bearer {token}":
        return _json(401, {"error": "unauthorized"})

    try:
        accounts = parse_accounts(json.loads(getattr(env, "ACCOUNTS_JSON", "") or "null"))
        url = api_url(getattr(env, "BALANCE_API_URL", None))
    except (ConfigError, ValueError):
        return _json(500, {"error": "accounts configuration invalid"})

    name = path[len("/check/"):]
    account = next((a for a in accounts if a.name == name), None)
    if account is None:
        return _json(404, {"error": "unknown account", "accounts": [a.name for a in accounts]})

    result = check_account(account, url)
    # 503 (not 4xx) so the platform treats a low balance like a real failure.
    status = 200 if result.ok else 503
    outcome = "ok" if result.ok else "failed"
    recovered = result.ok and previous == "failed"
    if record is not None and previous != outcome:
        record(outcome)
    return _json(status, {
        **result.as_json(),
        "rule": f"{account.value_path} {account.operator} {account.threshold}",
        **({"recovered": True} if recovered else {}),
    })


def parse_request(url: str, method: str, authorization: str) -> "_Request":
    """Minimal request view used by `handler`, kept runtime-agnostic."""
    path, _, _ = url.partition("?")
    return _Request(method=method, pathname=path, authorization=authorization)


class _Request:
    __slots__ = ("method", "pathname", "authorization")

    def __init__(self, *, method: str, pathname: str, authorization: str) -> None:
        self.method = method
        self.pathname = pathname
        self.authorization = authorization


def main():
    """Local smoke test: python3 worker_app.py <account> reads env, as the Worker would."""
    import os
    import sys

    account = sys.argv[1] if len(sys.argv) > 1 else ""
    env = os.environ
    request = parse_request(f"/check/{account}", "GET", env.get("Authorization", f"Bearer {env.get('CHECK_TOKEN', '')}"))
    status, _, body = handler(request, env)
    print(status, body)


if __name__ == "__main__":
    main()
