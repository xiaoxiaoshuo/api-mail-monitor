"""Poll a JSON balance endpoint and send alerts without logging private data."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parseaddr
from http.client import HTTPException
import json
import os
from queue import Empty, Queue
import socket
import sys
from threading import Thread
import time
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


RESEND_URL = "https://api.resend.com/emails"
REQUEST_TIMEOUT_SECONDS = 15
BALANCE_DEADLINE_SECONDS = 60
EMAIL_DEADLINE_SECONDS = 60
MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 65_536
BEIJING = timezone(timedelta(hours=8))


class ConfigError(Exception):
    """Invalid configuration; do not include secret values in this exception."""


class RequestFailure(Exception):
    """A sanitized failure, safe to include in a private alert email."""

    def __init__(self, reason: str, *, retryable: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


class MailFailure(Exception):
    """The email service did not confirm acceptance."""


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward either API credential to a redirected host.
        return None


OPENER = build_opener(NoRedirects())


@dataclass(frozen=True)
class Config:
    balance_url: str
    balance_token: str
    resend_key: str
    sender: str
    recipient: str
    threshold: Decimal


def read_config(environ: Mapping[str, str] | None = None) -> Config:
    env = os.environ if environ is None else environ

    def required(name: str) -> str:
        value = env.get(name, "").strip()
        if not value or "\r" in value or "\n" in value:
            raise ConfigError(f"Missing or invalid {name}.")
        return value

    balance_url = required("BALANCE_API_URL")
    try:
        parsed = urlsplit(balance_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError
        # Validate the port without exposing a malformed URL in an error.
        _ = parsed.port
    except ValueError:
        raise ConfigError("BALANCE_API_URL must be a valid HTTPS URL.") from None

    sender = required("ALERT_FROM")
    recipient = required("ALERT_TO")
    for value in (sender, recipient):
        _, address = parseaddr(value)
        if address.count("@") != 1 or any(c.isspace() for c in address):
            raise ConfigError("Invalid email address configuration.")
        local, domain = address.split("@")
        if not local or not domain or "," in value or ";" in value:
            raise ConfigError("Configure exactly one sender and one recipient.")

    try:
        threshold = Decimal(env.get("BALANCE_THRESHOLD", "1500"))
        if not threshold.is_finite() or threshold < 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise ConfigError("BALANCE_THRESHOLD must be finite and nonnegative.") from None

    return Config(
        balance_url=balance_url,
        balance_token=required("BALANCE_API_TOKEN"),
        resend_key=required("RESEND_API_KEY"),
        sender=sender,
        recipient=recipient,
        threshold=threshold,
    )


def reject_nonfinite(value: str):
    raise ValueError("Non-finite JSON number.")


def request_once(
    url: str,
    token: str,
    *,
    payload: dict | None = None,
    idempotency_key: str | None = None,
) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "api-mail-monitor/1.0",
    }
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    request = Request(
        url, data=body, headers=headers, method="POST" if body is not None else "GET"
    )
    try:
        with OPENER.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        raise RequestFailure(
            f"HTTP {status}",
            retryable=status in (408, 425, 429) or 500 <= status <= 599,
        ) from None
    except (TimeoutError, socket.timeout):
        raise RequestFailure("请求超时", retryable=True) from None
    except (URLError, OSError, HTTPException):
        raise RequestFailure("网络、DNS、TLS 连接失败或 HTTP 响应中断", retryable=True) from None

    if len(raw) > MAX_RESPONSE_BYTES:
        raise RequestFailure("响应超过大小限制")
    try:
        result = json.loads(
            raw.decode("utf-8"), parse_float=Decimal, parse_constant=reject_nonfinite
        )
    except (ValueError, UnicodeError, InvalidOperation, RecursionError):
        raise RequestFailure("响应不是有效 JSON") from None
    if not isinstance(result, dict):
        raise RequestFailure("响应不是 JSON 对象")
    return result


def request_json(url: str, token: str, **kwargs) -> dict:
    for attempt in range(MAX_ATTEMPTS):
        try:
            return request_once(url, token, **kwargs)
        except RequestFailure as error:
            if not error.retryable or attempt == MAX_ATTEMPTS - 1:
                raise
            time.sleep(2**attempt)
    raise AssertionError("Unreachable retry state.")


def call_with_deadline(operation, timeout_seconds: float):
    """Bound total wall time, not just socket inactivity (also works on Windows)."""
    completed = Queue(maxsize=1)

    def worker():
        try:
            completed.put((True, operation()))
        except Exception as error:
            completed.put((False, error))

    # A stuck socket must not prevent shutdown. A timed-out balance worker
    # can finish later, but it only reads the API and never sends mail itself.
    Thread(target=worker, daemon=True).start()
    try:
        succeeded, value = completed.get(timeout=timeout_seconds)
    except Empty:
        raise RequestFailure("请求超过总耗时限制") from None
    if not succeeded:
        raise value
    return value


def fetch_balance(config: Config) -> Decimal:
    result = request_json(config.balance_url, config.balance_token)
    value = result.get("balance")
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise RequestFailure("余额字段缺失或不是数值")
    try:
        balance = Decimal(str(value))
        if not balance.is_finite():
            raise InvalidOperation
    except (ValueError, InvalidOperation):
        raise RequestFailure("余额不是有限数值") from None
    return balance


def send_email(config: Config, subject: str, text: str) -> None:
    # One stable key/body for all HTTP retries of this particular email.
    # A new monitoring run deliberately gets a new key: no alert cooldown.
    key = "balance-monitor-" + str(uuid.uuid4())
    try:
        result = call_with_deadline(
            lambda: request_json(
                RESEND_URL,
                config.resend_key,
                payload={
                    "from": config.sender,
                    "to": [config.recipient],
                    "subject": subject,
                    "text": text,
                },
                idempotency_key=key,
            ),
            EMAIL_DEADLINE_SECONDS,
        )
    except RequestFailure:
        raise MailFailure("Email service did not accept the message.") from None
    if not isinstance(result.get("id"), str) or not result["id"].strip():
        raise MailFailure("Email service did not return a message ID.")


def display_number(value: Decimal) -> str:
    # Fixed-point formatting of a compact exponent such as 1e-1000000000
    # can allocate gigabytes. Keep email formatting bounded instead.
    if -6 <= value.adjusted() <= 12 and value.as_tuple().exponent >= -6:
        return format(value, "f")
    return format(value, ".12E")


def run_monitor(config: Config, *, mode: str = "check") -> str:
    if mode not in ("check", "test-email"):
        raise ConfigError("Unknown monitor mode.")
    checked_at = datetime.now(BEIJING).isoformat(timespec="seconds")
    prefix = (
        f"检查时间（北京时间）：{checked_at}\n"
        f"报警阈值：剩余积分低于 {display_number(config.threshold)}\n"
    )
    try:
        balance = call_with_deadline(lambda: fetch_balance(config), BALANCE_DEADLINE_SECONDS)
    except RequestFailure as error:
        subject = "【余额监控】接口不可用"
        if mode == "test-email":
            subject = "【余额监控】测试邮件：接口不可用"
        send_email(
            config,
            subject,
            prefix
            + f"\n本次无法读取剩余积分。原因：{error.reason}\n"
            + "超时、网络错误、限流和服务端错误会先进行有限次数重试。\n"
            + "异常持续时，每次监控运行都会发送报警，不做去重。\n",
        )
        return "unavailable-alert"

    if mode == "test-email":
        send_email(
            config,
            "【余额监控】工作流测试邮件",
            prefix
            + f"\n当前剩余积分：{display_number(balance)}\n"
            + "接口请求成功。这是一次手动测试，不代表定时监控已启用。\n",
        )
        return "test-email"

    if balance < config.threshold:
        send_email(
            config,
            "【余额监控】剩余积分不足",
            prefix
            + f"\n当前剩余积分：{display_number(balance)}\n"
            + "余额已低于阈值，请及时检查账户或补充积分。\n"
            + "只要余额仍低于阈值，每次监控运行都会发送报警。\n",
        )
        return "low-balance-alert"
    return "healthy"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("check", "test-email"), default="check")
    args = parser.parse_args(argv)
    try:
        outcome = run_monitor(read_config(), mode=args.mode)
    except ConfigError:
        print("Configuration invalid; check repository secrets and variables.", file=sys.stderr)
        return 1
    except MailFailure:
        print("Email submission failed; check the email service dashboard.", file=sys.stderr)
        return 1
    except Exception:
        # Exceptions from HTTP libraries may contain URLs or credentials.
        # No tracebacks, response bodies, addresses, or balances in public logs.
        print("Monitor failed; sensitive diagnostic details are omitted.", file=sys.stderr)
        return 1

    if outcome == "healthy":
        print("Check completed; no alert needed.")
    elif outcome == "test-email":
        print("Test email accepted by email service.")
    else:
        print("Alert accepted by email service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
