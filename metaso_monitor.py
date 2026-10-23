"""Check multiple Metaso API accounts and write a sanitized local report."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_URL = "https://metaso.cn/api/v1/usage"
DEFAULT_ACCOUNTS_FILE = "~/.config/metaso-monitor/accounts.json"
TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 65536


class MonitorConfigError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirects())


@dataclass(frozen=True)
class Account:
    label: str
    key: str


@dataclass(frozen=True)
class Config:
    accounts: tuple[Account, ...]
    url: str = DEFAULT_URL
    threshold: Decimal = Decimal("0")


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\r" in value or "\n" in value:
        raise MonitorConfigError(f"Invalid {name}.")
    return value.strip()


def load_accounts(path: str | None = None, raw_json: str | None = None) -> tuple[Account, ...]:
    if raw_json is None:
        account_path = Path(os.path.expanduser(path or os.getenv("METASO_ACCOUNTS_FILE", DEFAULT_ACCOUNTS_FILE)))
        try:
            raw_json = account_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MonitorConfigError("Accounts file is missing or unreadable.") from exc
    try:
        data = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise MonitorConfigError("Accounts configuration is not valid JSON.") from exc
    if not isinstance(data, list) or not data:
        raise MonitorConfigError("Accounts configuration must be a non-empty list.")
    accounts: list[Account] = []
    for item in data:
        if not isinstance(item, dict):
            raise MonitorConfigError("Each account must be an object.")
        label = _required_string(item.get("label"), "account label")
        key = _required_string(item.get("key"), "account key")
        if not key.startswith("mk-"):
            raise MonitorConfigError("Account key has an invalid format.")
        accounts.append(Account(label=label, key=key))
    if len({a.label for a in accounts}) != len(accounts):
        raise MonitorConfigError("Account labels must be unique.")
    return tuple(accounts)


def read_config(environ: dict[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ
    raw = env.get("METASO_ACCOUNTS_JSON") or None
    accounts = load_accounts(env.get("METASO_ACCOUNTS_FILE"), raw)
    url = env.get("METASO_API_URL", DEFAULT_URL).strip()
    if not url.startswith("https://") or "\r" in url or "\n" in url:
        raise MonitorConfigError("METASO_API_URL must be an HTTPS URL.")
    try:
        threshold = Decimal(env.get("METASO_THRESHOLD", "0"))
        if not threshold.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise MonitorConfigError("METASO_THRESHOLD must be finite.") from None
    return Config(accounts=accounts, url=url, threshold=threshold)


def request_value(account: Account, url: str) -> Any:
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {account.key}",
            "Accept": "application/json",
            "User-Agent": "metaso-multi-account-monitor/1.0",
        },
        method="GET",
    )
    try:
        with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from None
    except (TimeoutError, URLError, OSError):
        raise RuntimeError("network or timeout failure") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("response too large")
    try:
        return json.loads(raw.decode("utf-8"), parse_float=Decimal)
    except (ValueError, UnicodeError, InvalidOperation, RecursionError):
        raise RuntimeError("invalid JSON response") from None


def _find_number(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            number = Decimal(str(value))
            return number if number.is_finite() else None
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, dict):
        for key in ("balance", "remaining", "rest", "credits", "quota"):
            if key in value:
                found = _find_number(value[key])
                if found is not None:
                    return found
        for key in ("data", "externalPackage"):
            if key in value:
                found = _find_number(value[key])
                if found is not None:
                    return found
    return None


def check_account(account: Account, config: Config) -> dict[str, Any]:
    try:
        raw = request_value(account, config.url)
        balance = _find_number(raw)
        if balance is None:
            return {"label": account.label, "status": "invalid-response", "reason": "no numeric balance"}
        if balance <= config.threshold:
            return {"label": account.label, "status": "low", "balance": str(balance)}
        return {"label": account.label, "status": "healthy", "balance": str(balance)}
    except RuntimeError as exc:
        return {"label": account.label, "status": "unavailable", "reason": str(exc)}


def run(config: Config) -> dict[str, Any]:
    results = [check_account(account, config) for account in config.accounts]
    alert_results = [r for r in results if r["status"] != "healthy"]
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "threshold": str(config.threshold),
        "alert": bool(alert_results),
        "accounts": results,
    }


def write_report(report: dict[str, Any], path: str) -> None:
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    try:
        target.chmod(0o600)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-file", default="~/.config/metaso-monitor/latest-report.json")
    args = parser.parse_args(argv)
    try:
        report = run(read_config())
        write_report(report, args.report_file)
    except MonitorConfigError:
        print("Monitor configuration invalid; sensitive details omitted.", file=sys.stderr)
        return 1
    except Exception:
        print("Monitor failed; sensitive details omitted.", file=sys.stderr)
        return 1
    print("Alert report written." if report["alert"] else "All monitored accounts are healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
