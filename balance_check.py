"""Aggregate several balance checks into one decision per task.

The yunji-task platform runs one HTTP task per account, so this module is the
single endpoint every account task calls: it reads that account's balance,
compares it with the account's threshold and answers with an HTTP status the
platform already understands (2xx healthy, non-2xx failure), while putting the
exact numbers into the JSON body so an alert template can render `r.*`.

Recovery is *not* decided here: the platform remembers whether the task was
failing (`notifyOnRecover`). Keeping one decision per HTTP call is what keeps
this stateless, so it can run on Cloudflare Workers without a database.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit

from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.error import HTTPError, URLError

DEFAULT_URL = "https://metaso.cn/api/v1/usage"
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 65536

# Field paths this service understands, e.g. `data.balance` or `items[0].credits`.
_PATH_RE = re.compile(r"^(?:[^\s.\[\]]+|\[\d+\])(?:\.[^\s.\[\]]+|\[\d+\])*$")
COMPARISONS = ("lt", "lte", "gt", "gte", "eq", "ne", "between")
COMPARISON_LABELS = {
    "lt": "低于", "lte": "小于等于", "gt": "高于", "gte": "大于等于",
    "eq": "等于", "ne": "不等于", "between": "介于（含边界）",
}
# Only these field names are considered a balance, in priority order.
_JSON_KEYS = ("balance", "remaining", "rest", "credits", "quota")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Account:
    name: str
    key: str
    threshold: Decimal
    operator: str = "gte"
    threshold_to: Decimal | None = None
    value_path: str = "data.balance"


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    value: Decimal | None
    threshold: Decimal | None
    error: str | None
    http_status: int | None

    def as_json(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "account": self.name,
            "ok": self.ok,
            # Numbers are emitted as strings to keep exact decimal digits.
            "value": None if self.value is None else str(self.value),
            "threshold": None if self.threshold is None else str(self.threshold),
        }
        if self.error:
            body["error"] = self.error
        return body


def _clean(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
        raise ConfigError(f"Invalid {name}.")
    return value.strip()


def _decimal(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ConfigError(f"Invalid {name}.") from None
    if not number.is_finite():
        raise ConfigError(f"Invalid {name}.")
    return number


def _valid_path(path: str) -> bool:
    return bool(_PATH_RE.match(path))


def parse_accounts(raw: Any) -> list[Account]:
    """Validate the accounts document; never echo key material in errors."""
    if not isinstance(raw, list) or not raw:
        raise ConfigError("accounts must be a non-empty list.")
    accounts: list[Account] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("each account must be an object.")
        key = _clean(item.get("key"), "account key")
        if not key.startswith("mk-"):
            raise ConfigError("account key has an invalid format.")
        operator = str(item.get("operator", "gte") or "gte").strip()
        if operator not in COMPARISONS:
            raise ConfigError("account operator is not supported.")
        threshold = _decimal(item.get("threshold", 1500), "account threshold")
        threshold_to = item.get("thresholdTo")
        if threshold_to is not None:
            threshold_to = _decimal(threshold_to, "account thresholdTo")
            if threshold_to < threshold:
                raise ConfigError("account thresholdTo must not be below threshold.")
        elif operator == "between":
            raise ConfigError("between requires thresholdTo.")
        path = str(item.get("path", "data.balance") or "data.balance").strip()
        if not _valid_path(path):
            raise ConfigError("account path is invalid.")
        accounts.append(Account(
            name=_clean(item.get("name") or item.get("label"), "account name"),
            key=key,
            threshold=threshold,
            operator=operator,
            threshold_to=threshold_to if operator == "between" else None,
            value_path=path,
        ))
    if len({a.name for a in accounts}) != len(accounts):
        raise ConfigError("account names must be unique.")
    return accounts


def api_url(raw: str | None) -> str:
    url = (raw or DEFAULT_URL).strip()
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise ConfigError("API URL must be a valid HTTPS URL.") from None
    return url


class NoRedirects(HTTPRedirectHandler):
    """Never follow redirects: a redirected request could leak the bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirects())


def fetch_json(url: str, key: str, *, opener=OPENER, timeout: float = TIMEOUT_SECONDS) -> Any:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "yunji-task-balance-check/1.0",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from None
    except (TimeoutError, URLError, OSError):
        raise RuntimeError("network or timeout failure") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("response too large")
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal, parse_int=Decimal)
    except (ValueError, UnicodeError, RecursionError):
        raise RuntimeError("invalid JSON response") from None


def _parts(path: str) -> list[str]:
    normalized = path[2:] if path.startswith("$.") else path[1:] if path.startswith("$[") else path
    return normalized.replace("[", ".").replace("]", "").split(".")


def field_at(root: Any, path: str) -> Any:
    value = root
    for part in _parts(path):
        if isinstance(value, dict):
            if part not in value:
                return None
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def balance_at(raw: Any, path: str) -> Decimal | None:
    """Path value if numeric, else the first known balance field in the same document."""
    candidate = _to_decimal(field_at(raw, path))
    if candidate is not None:
        return candidate
    return _discover_number(raw)


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return number if number.is_finite() else None
    return None


def _discover_number(value: Any) -> Decimal | None:
    if isinstance(value, dict):
        for name in _JSON_KEYS:
            if name in value:
                found = _to_decimal(value[name])
                if found is not None:
                    return found
        for name in ("data", "externalPackage"):
            if name in value:
                found = _discover_number(value[name])
                if found is not None:
                    return found
    return None


def satisfies(value: Decimal, account: Account) -> bool:
    op = account.operator
    if op == "lt":
        return value < account.threshold
    if op == "lte":
        return value <= account.threshold
    if op == "gt":
        return value > account.threshold
    if op == "gte":
        return value >= account.threshold
    if op == "eq":
        return value == account.threshold
    if op == "ne":
        return value != account.threshold
    return account.threshold <= value <= (account.threshold_to or account.threshold)


def describe(account: Account) -> str:
    bound = (
        f"{account.threshold}–{account.threshold_to}"
        if account.operator == "between"
        else str(account.threshold)
    )
    return f"{account.value_path} {COMPARISON_LABELS[account.operator]} {bound}"


def check_account(account: Account, url: str, *, fetch=fetch_json) -> CheckResult:
    try:
        raw = fetch(url, account.key)
    except RuntimeError as exc:
        return CheckResult(account.name, False, None, account.threshold, str(exc), None)
    value = balance_at(raw, account.value_path)
    if value is None:
        return CheckResult(account.name, False, None, account.threshold, "no numeric balance", None)
    if not satisfies(value, account):
        return CheckResult(
            account.name, False, value, account.threshold,
            f"阈值未满足：{account.value_path} = {value}，期望 {describe(account)}", None,
        )
    return CheckResult(account.name, True, value, account.threshold, None, None)


def _display(value: Decimal | None) -> str:
    if value is None:
        return "—"
    if -6 <= value.adjusted() <= 12:
        return format(value, "f")
    return format(value, ".12E")


def summary(results: list[CheckResult]) -> str:
    """Multi-account digest for the alert body, one line per account."""
    lines = [f"{'✅' if r.ok else '❌'} {r.name}：{_display(r.value)}（阈值 {_display(r.threshold)}）" + (f" · {r.error}" if r.error else "") for r in results]
    return "\n".join(lines)


def run(accounts: list[Account], url: str, *, fetch=fetch_json) -> list[CheckResult]:
    return [check_account(account, url, fetch=fetch) for account in accounts]
