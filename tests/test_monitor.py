from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.client import IncompleteRead
import io
import json
import os
from threading import Event
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

import monitor


ENV = {
    "BALANCE_API_URL": "https://balance.example.invalid/credits",
    "BALANCE_API_TOKEN": "test-balance-token-not-a-real-key",
    "RESEND_API_KEY": "test-mail-token-not-a-real-key",
    "ALERT_FROM": "Monitor <alerts@example.invalid>",
    "ALERT_TO": "recipient@example.invalid",
    "BALANCE_THRESHOLD": "500",
}
CONFIG = monitor.read_config(ENV)


class ConfigurationTests(unittest.TestCase):
    def test_valid_configuration(self):
        self.assertEqual(CONFIG.threshold, Decimal("500"))

    def test_default_threshold(self):
        env = {key: value for key, value in ENV.items() if key != "BALANCE_THRESHOLD"}
        self.assertEqual(monitor.read_config(env).threshold, Decimal("1500"))

    def test_missing_secrets_fail_closed(self):
        for name in ENV.keys() - {"BALANCE_THRESHOLD"}:
            with self.subTest(name=name):
                env = dict(ENV)
                env[name] = ""
                with self.assertRaises(monitor.ConfigError):
                    monitor.read_config(env)

    def test_invalid_urls(self):
        for url in (
            "http://example.invalid/credits",
            "https:///credits",
            "https://user:password@example.invalid/credits",
            "https://example.invalid/credits#fragment",
            "https://example.invalid:bad/credits",
        ):
            with self.subTest(url=url):
                with self.assertRaises(monitor.ConfigError):
                    monitor.read_config({**ENV, "BALANCE_API_URL": url})

    def test_invalid_thresholds(self):
        for value in ("NaN", "Infinity", "-1", "wrong", ""):
            with self.subTest(value=value):
                with self.assertRaises(monitor.ConfigError):
                    monitor.read_config({**ENV, "BALANCE_THRESHOLD": value})

    def test_invalid_addresses(self):
        for value in ("not-an-address", "@example.invalid", "a@", "a@b.invalid,c@d.invalid"):
            with self.subTest(value=value):
                with self.assertRaises(monitor.ConfigError):
                    monitor.read_config({**ENV, "ALERT_TO": value})

    def test_header_injection_is_rejected(self):
        with self.assertRaises(monitor.ConfigError):
            monitor.read_config({**ENV, "BALANCE_API_TOKEN": "test\r\nInjected: value"})


class HttpTests(unittest.TestCase):
    def response(self, raw):
        response = MagicMock()
        response.read.return_value = raw
        response.__enter__.return_value = response
        return response

    def test_get_parses_decimal_and_sets_timeout(self):
        with patch.object(monitor.OPENER, "open", return_value=self.response(b'{"balance": 42.12}')) as opened:
            result = monitor.request_once(CONFIG.balance_url, CONFIG.balance_token)
        self.assertEqual(result["balance"], Decimal("42.12"))
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + CONFIG.balance_token)
        self.assertEqual(opened.call_args.kwargs["timeout"], 15)

    def test_post_payload_and_idempotency(self):
        payload = {"subject": "测试", "text": "测试正文"}
        with patch.object(monitor.OPENER, "open", return_value=self.response(b'{"id":"test-message"}')) as opened:
            monitor.request_once(monitor.RESEND_URL, CONFIG.resend_key, payload=payload, idempotency_key="test-id")
        request = opened.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), payload)
        self.assertEqual(request.get_header("Idempotency-key"), "test-id")

    def test_invalid_json_and_non_objects(self):
        for raw in (b"not-json", b"[]", b"null", b"\xff", b'{"balance":NaN}', b'{"balance":Infinity}', b'{"balance":1e999999999999999999999999999999}', b"[" * 1500 + b"0" + b"]" * 1500):
            with self.subTest(raw=raw):
                with patch.object(monitor.OPENER, "open", return_value=self.response(raw)):
                    with self.assertRaises(monitor.RequestFailure):
                        monitor.request_once(CONFIG.balance_url, CONFIG.balance_token)

    def test_large_response_is_rejected(self):
        with patch.object(monitor.OPENER, "open", return_value=self.response(b"x" * (monitor.MAX_RESPONSE_BYTES + 1))):
            with self.assertRaises(monitor.RequestFailure):
                monitor.request_once(CONFIG.balance_url, CONFIG.balance_token)

    def test_http_error_classification(self):
        for status, retryable in ((302, False), (401, False), (403, False), (404, False), (408, True), (425, True), (429, True), (500, True), (503, True)):
            with self.subTest(status=status):
                error = HTTPError(CONFIG.balance_url, status, "private-response", {}, io.BytesIO(b"secret"))
                with patch.object(monitor.OPENER, "open", side_effect=error):
                    with self.assertRaises(monitor.RequestFailure) as caught:
                        monitor.request_once(CONFIG.balance_url, CONFIG.balance_token)
                self.assertEqual(caught.exception.reason, f"HTTP {status}")
                self.assertEqual(caught.exception.retryable, retryable)

    def test_network_errors_are_sanitized(self):
        for error in (TimeoutError("secret"), URLError("secret"), OSError("secret"), IncompleteRead(b"secret", 100)):
            with self.subTest(error=type(error).__name__):
                with patch.object(monitor.OPENER, "open", side_effect=error):
                    with self.assertRaises(monitor.RequestFailure) as caught:
                        monitor.request_once(CONFIG.balance_url, CONFIG.balance_token)
                self.assertTrue(caught.exception.retryable)
                self.assertNotIn("secret", str(caught.exception))

    def test_redirects_are_refused(self):
        handler = monitor.NoRedirects()
        result = handler.redirect_request(Request(CONFIG.balance_url), None, 302, "redirect", {}, "https://other.example.invalid/")
        self.assertIsNone(result)

    def test_retry_then_success(self):
        with patch("monitor.request_once", side_effect=[monitor.RequestFailure("HTTP 503", retryable=True), {"balance": 1000}]) as request:
            with patch("monitor.time.sleep") as sleep:
                self.assertEqual(monitor.request_json(CONFIG.balance_url, CONFIG.balance_token), {"balance": 1000})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_retries_are_bounded(self):
        with patch("monitor.request_once", side_effect=monitor.RequestFailure("timeout", retryable=True)) as request:
            with patch("monitor.time.sleep") as sleep:
                with self.assertRaises(monitor.RequestFailure):
                    monitor.request_json(CONFIG.balance_url, CONFIG.balance_token)
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_permanent_error_not_retried(self):
        with patch("monitor.request_once", side_effect=monitor.RequestFailure("HTTP 401")) as request:
            with patch("monitor.time.sleep") as sleep:
                with self.assertRaises(monitor.RequestFailure):
                    monitor.request_json(CONFIG.balance_url, CONFIG.balance_token)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()


class DeadlineTests(unittest.TestCase):
    def test_successful_operation_returns_value(self):
        self.assertEqual(monitor.call_with_deadline(lambda: 42, 1), 42)

    def test_operation_exception_is_propagated(self):
        def fail():
            raise monitor.RequestFailure("HTTP 503")
        with self.assertRaises(monitor.RequestFailure):
            monitor.call_with_deadline(fail, 1)

    def test_blocking_operation_has_a_wall_clock_deadline(self):
        gate = Event()
        try:
            with self.assertRaises(monitor.RequestFailure) as caught:
                monitor.call_with_deadline(lambda: gate.wait(2), 0.01)
            self.assertIn("总耗时", caught.exception.reason)
        finally:
            gate.set()

    def test_stalled_balance_fetch_still_sends_an_alert(self):
        gate = Event()
        def stalled_fetch(config):
            gate.wait(2)
            return Decimal("1000")
        try:
            with patch("monitor.fetch_balance", side_effect=stalled_fetch):
                with patch("monitor.BALANCE_DEADLINE_SECONDS", 0.01):
                    with patch("monitor.send_email") as send:
                        outcome = monitor.run_monitor(CONFIG)
            self.assertEqual(outcome, "unavailable-alert")
            send.assert_called_once()
            self.assertIn("总耗时", send.call_args.args[2])
        finally:
            gate.set()

    def test_stalled_mail_service_returns_failure(self):
        gate = Event()
        def stalled_mail(*args, **kwargs):
            gate.wait(2)
            return {"id": "test-message"}
        try:
            with patch("monitor.request_json", side_effect=stalled_mail):
                with patch("monitor.EMAIL_DEADLINE_SECONDS", 0.01):
                    with self.assertRaises(monitor.MailFailure):
                        monitor.send_email(CONFIG, "test subject", "test text")
        finally:
            gate.set()


class BalanceTests(unittest.TestCase):
    def test_numeric_balances(self):
        for value in (0, -1, 500, 1.25, Decimal("499.99")):
            with self.subTest(value=value):
                with patch("monitor.request_json", return_value={"balance": value}):
                    self.assertEqual(monitor.fetch_balance(CONFIG), Decimal(str(value)))

    def test_missing_or_invalid_balances(self):
        for payload in ({}, {"error": "no balance"}, {"balance": True}, {"balance": None}, {"balance": "499"}, {"balance": []}, {"balance": float("inf")}, {"balance": Decimal("NaN")}):
            with self.subTest(payload=payload):
                with patch("monitor.request_json", return_value=payload):
                    with self.assertRaises(monitor.RequestFailure):
                        monitor.fetch_balance(CONFIG)


class MonitorTests(unittest.TestCase):
    def test_threshold_is_strictly_less_than(self):
        for value, should_alert in (("500", False), ("500.01", False), ("499.99", True), ("0", True), ("-1", True)):
            with self.subTest(value=value):
                with patch("monitor.fetch_balance", return_value=Decimal(value)):
                    with patch("monitor.send_email") as send:
                        result = monitor.run_monitor(CONFIG)
                self.assertEqual(send.call_count, int(should_alert))
                self.assertEqual(result, "low-balance-alert" if should_alert else "healthy")

    def test_production_threshold_1500_is_strict(self):
        config = monitor.read_config({**ENV, "BALANCE_THRESHOLD": "1500"})
        for value, should_alert in (("1499.99", True), ("1500", False), ("1500.01", False)):
            with self.subTest(value=value):
                with patch("monitor.fetch_balance", return_value=Decimal(value)):
                    with patch("monitor.send_email") as send:
                        outcome = monitor.run_monitor(config)
                self.assertEqual(send.call_count, int(should_alert))
                self.assertEqual(outcome, "low-balance-alert" if should_alert else "healthy")
                if should_alert:
                    self.assertIn("1500", send.call_args.args[2])

    def test_unavailable_endpoint_alerts_with_production_threshold(self):
        config = monitor.read_config({**ENV, "BALANCE_THRESHOLD": "1500"})
        with patch("monitor.fetch_balance", side_effect=monitor.RequestFailure("HTTP 503")):
            with patch("monitor.send_email") as send:
                outcome = monitor.run_monitor(config)
        self.assertEqual(outcome, "unavailable-alert")
        send.assert_called_once()
        self.assertIn("1500", send.call_args.args[2])
        self.assertIn("HTTP 503", send.call_args.args[2])

    def test_extreme_balances_keep_email_bounded(self):
        cases = (
            (Decimal("1e-999999999999999999"), "check"),
            (Decimal("-1e+999999999999999999"), "check"),
            (Decimal("1e+999999999999999999"), "test-email"),
            (Decimal("1." + "0" * 10_000 + "1"), "check"),
        )
        for balance, mode in cases:
            with self.subTest(mode=mode, exponent=balance.as_tuple().exponent):
                with patch("monitor.fetch_balance", return_value=balance):
                    with patch("monitor.send_email") as send:
                        monitor.run_monitor(CONFIG, mode=mode)
                send.assert_called_once()
                self.assertLess(len(send.call_args.args[2]), 1000)

    def test_extreme_threshold_keeps_email_bounded(self):
        config = replace(CONFIG, threshold=Decimal("1e+999999999999999999"))
        with patch("monitor.fetch_balance", return_value=Decimal("1")):
            with patch("monitor.send_email") as send:
                monitor.run_monitor(config)
        self.assertLess(len(send.call_args.args[2]), 1000)

    def test_regular_number_display_preserves_precision(self):
        for value in ("1234.56", "499.99", "500", "0", "-1"):
            self.assertEqual(monitor.display_number(Decimal(value)), value)

    def test_custom_threshold(self):
        with patch("monitor.fetch_balance", return_value=Decimal("700")):
            with patch("monitor.send_email") as send:
                monitor.run_monitor(replace(CONFIG, threshold=Decimal("800")))
        send.assert_called_once()

    def test_repeated_low_balance_alerts_every_run(self):
        with patch("monitor.fetch_balance", return_value=Decimal("1")):
            with patch("monitor.send_email") as send:
                monitor.run_monitor(CONFIG)
                monitor.run_monitor(CONFIG)
        self.assertEqual(send.call_count, 2)

    def test_unavailable_endpoint_sends_error_email_every_run(self):
        with patch("monitor.fetch_balance", side_effect=monitor.RequestFailure("HTTP 503")):
            with patch("monitor.send_email") as send:
                result = monitor.run_monitor(CONFIG)
                monitor.run_monitor(CONFIG)
        self.assertEqual(result, "unavailable-alert")
        self.assertEqual(send.call_count, 2)
        self.assertIn("HTTP 503", send.call_args.args[2])
        self.assertNotIn(CONFIG.balance_token, send.call_args.args[2])

    def test_low_balance_email_contains_private_balance(self):
        with patch("monitor.fetch_balance", return_value=Decimal("123.45")):
            with patch("monitor.send_email") as send:
                monitor.run_monitor(CONFIG)
        self.assertIn("123.45", send.call_args.args[2])
        self.assertIn("+08:00", send.call_args.args[2])

    def test_test_mode_sends_even_when_healthy(self):
        with patch("monitor.fetch_balance", return_value=Decimal("1000")):
            with patch("monitor.send_email") as send:
                result = monitor.run_monitor(CONFIG, mode="test-email")
        self.assertEqual(result, "test-email")
        send.assert_called_once()

    def test_test_mode_reports_api_failure(self):
        with patch("monitor.fetch_balance", side_effect=monitor.RequestFailure("HTTP 403")):
            with patch("monitor.send_email") as send:
                result = monitor.run_monitor(CONFIG, mode="test-email")
        self.assertEqual(result, "unavailable-alert")
        self.assertIn("测试", send.call_args.args[1])

    def test_unknown_mode_fails_before_network_call(self):
        with patch("monitor.fetch_balance") as fetch:
            with self.assertRaises(monitor.ConfigError):
                monitor.run_monitor(CONFIG, mode="other")
        fetch.assert_not_called()


class CooldownTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_file = os.path.join(self.directory.name, 'state.json')
        self.config = replace(CONFIG, cooldown_minutes=60, state_file=self.state_file)

    def write_last_alert(self, kind, minutes_ago):
        moment = datetime.now(monitor.BEIJING) - timedelta(minutes=minutes_ago)
        with open(self.state_file, 'w', encoding='utf-8') as handle:
            json.dump({'alert_kind': kind, 'active': True, 'last_alert_at': moment.isoformat(timespec='seconds')}, handle)

    def test_first_low_balance_alert_is_sent(self):
        with patch('monitor.fetch_balance', return_value=Decimal('100')):
            with patch('monitor.send_email') as send:
                outcome = monitor.run_monitor(self.config)
        self.assertEqual(outcome, 'low-balance-alert')
        send.assert_called_once()

    def test_repeat_within_cooldown_is_suppressed(self):
        with patch('monitor.fetch_balance', return_value=Decimal('100')):
            with patch('monitor.send_email') as send:
                monitor.run_monitor(self.config)
                second = monitor.run_monitor(self.config)
        self.assertEqual(second, 'alert-suppressed')
        self.assertEqual(send.call_count, 1)

    def test_unavailable_repeats_are_suppressed_too(self):
        with patch('monitor.fetch_balance', side_effect=monitor.RequestFailure('HTTP 503')):
            with patch('monitor.send_email') as send:
                monitor.run_monitor(self.config)
                second = monitor.run_monitor(self.config)
        self.assertEqual(second, 'alert-suppressed')
        self.assertEqual(send.call_count, 1)

    def test_alert_resent_after_cooldown_expires(self):
        self.write_last_alert('low-balance', 61)
        with patch('monitor.fetch_balance', return_value=Decimal('100')):
            with patch('monitor.send_email') as send:
                outcome = monitor.run_monitor(self.config)
        self.assertEqual(outcome, 'low-balance-alert')
        send.assert_called_once()

    def test_changed_alert_kind_bypasses_cooldown(self):
        self.write_last_alert('low-balance', 1)
        with patch('monitor.fetch_balance', side_effect=monitor.RequestFailure('HTTP 503')):
            with patch('monitor.send_email') as send:
                outcome = monitor.run_monitor(self.config)
        self.assertEqual(outcome, 'unavailable-alert')
        send.assert_called_once()

    def test_recovery_notice_sent_once(self):
        with patch('monitor.fetch_balance', return_value=Decimal('100')):
            with patch('monitor.send_email'):
                monitor.run_monitor(self.config)
        with patch('monitor.fetch_balance', return_value=Decimal('5000')):
            with patch('monitor.send_email') as send:
                first = monitor.run_monitor(self.config)
                second = monitor.run_monitor(self.config)
        self.assertEqual(first, 'recovery-alert')
        self.assertEqual(second, 'healthy')
        self.assertEqual(send.call_count, 1)
        self.assertIn('已恢复正常', send.call_args.args[1])

    def test_healthy_without_prior_alert_sends_nothing(self):
        with patch('monitor.fetch_balance', return_value=Decimal('5000')):
            with patch('monitor.send_email') as send:
                outcome = monitor.run_monitor(self.config)
        self.assertEqual(outcome, 'healthy')
        send.assert_not_called()

    def test_corrupt_state_still_alerts(self):
        with open(self.state_file, 'w', encoding='utf-8') as handle:
            handle.write('not json')
        with patch('monitor.fetch_balance', return_value=Decimal('100')):
            with patch('monitor.send_email') as send:
                outcome = monitor.run_monitor(self.config)
        self.assertEqual(outcome, 'low-balance-alert')
        send.assert_called_once()

    def test_unwritable_state_still_alerts(self):
        blocker = os.path.join(self.directory.name, 'blocker')
        with open(blocker, 'w', encoding='utf-8') as handle:
            handle.write('x')
        config = replace(self.config, state_file=os.path.join(blocker, 'state.json'))
        with patch('monitor.fetch_balance', return_value=Decimal('100')):
            with patch('monitor.send_email') as send:
                outcome = monitor.run_monitor(config)
        self.assertEqual(outcome, 'low-balance-alert')
        send.assert_called_once()

    def test_zero_cooldown_alerts_every_run(self):
        config = replace(self.config, cooldown_minutes=0)
        with patch('monitor.fetch_balance', return_value=Decimal('100')):
            with patch('monitor.send_email') as send:
                monitor.run_monitor(config)
                monitor.run_monitor(config)
        self.assertEqual(send.call_count, 2)

    def test_no_state_file_disables_recovery(self):
        config = replace(self.config, state_file=None)
        with patch('monitor.fetch_balance', return_value=Decimal('100')):
            with patch('monitor.send_email'):
                monitor.run_monitor(config)
        with patch('monitor.fetch_balance', return_value=Decimal('5000')):
            with patch('monitor.send_email') as send:
                outcome = monitor.run_monitor(config)
        self.assertEqual(outcome, 'healthy')
        send.assert_not_called()

    def test_invalid_cooldown_configuration(self):
        for value in ('-1', 'abc', '1.5'):
            with self.subTest(value=value):
                with self.assertRaises(monitor.ConfigError):
                    monitor.read_config({**ENV, 'ALERT_COOLDOWN_MINUTES': value})

    def test_cooldown_and_state_file_parsed(self):
        config = monitor.read_config({**ENV, 'ALERT_COOLDOWN_MINUTES': '60', 'ALERT_STATE_FILE': ' /tmp/state.json '})
        self.assertEqual(config.cooldown_minutes, 60)
        self.assertEqual(config.state_file, '/tmp/state.json')


class MailTests(unittest.TestCase):
    def test_mail_parameters_and_idempotency_key(self):
        with patch("monitor.request_json", return_value={"id": "test-message"}) as request:
            monitor.send_email(CONFIG, "test subject", "test text")
        args = request.call_args
        self.assertEqual(args.args, (monitor.RESEND_URL, CONFIG.resend_key))
        self.assertEqual(args.kwargs["payload"]["to"], [CONFIG.recipient])
        self.assertTrue(args.kwargs["idempotency_key"].startswith("balance-monitor-"))

    def test_one_idempotency_key_for_all_mail_retries(self):
        with patch("monitor.request_once", side_effect=[monitor.RequestFailure("HTTP 429", retryable=True), {"id": "test-message"}]) as request:
            with patch("monitor.time.sleep"):
                monitor.send_email(CONFIG, "test subject", "test text")
        keys = [call.kwargs["idempotency_key"] for call in request.call_args_list]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])

    def test_separate_runs_do_not_deduplicate_emails(self):
        with patch("monitor.request_json", return_value={"id": "test-message"}) as request:
            monitor.send_email(CONFIG, "test subject", "test text")
            monitor.send_email(CONFIG, "test subject", "test text")
        self.assertNotEqual(request.call_args_list[0].kwargs["idempotency_key"], request.call_args_list[1].kwargs["idempotency_key"])

    def test_missing_email_id_is_failure(self):
        for result in ({}, {"id": None}, {"id": ""}):
            with self.subTest(result=result):
                with patch("monitor.request_json", return_value=result):
                    with self.assertRaises(monitor.MailFailure):
                        monitor.send_email(CONFIG, "test subject", "test text")

    def test_email_api_failure_is_sanitized(self):
        with patch("monitor.request_json", side_effect=monitor.RequestFailure("HTTP 401")):
            with self.assertRaises(monitor.MailFailure):
                monitor.send_email(CONFIG, "test subject", "test text")


class LogSafetyTests(unittest.TestCase):
    def invoke(self, *, balance=None, failure=None, mail_failure=None):
        output = io.StringIO()
        with patch.dict(monitor.os.environ, ENV, clear=True):
            with patch("monitor.fetch_balance", return_value=balance, side_effect=failure):
                with patch("monitor.send_email", side_effect=mail_failure):
                    with redirect_stdout(output), redirect_stderr(output):
                        code = monitor.main([])
        return code, output.getvalue()

    def assert_private_values_absent(self, output):
        for value in (CONFIG.balance_url, CONFIG.balance_token, CONFIG.resend_key, CONFIG.sender, CONFIG.recipient, "123.45"):
            self.assertNotIn(value, output)

    def test_healthy_output_does_not_include_balance(self):
        code, output = self.invoke(balance=Decimal("1234.56"))
        self.assertEqual(code, 0)
        self.assertNotIn("1234.56", output)
        self.assert_private_values_absent(output)

    def test_alert_output_does_not_include_balance_or_address(self):
        code, output = self.invoke(balance=Decimal("123.45"))
        self.assertEqual(code, 0)
        self.assert_private_values_absent(output)

    def test_unavailable_api_is_success_if_alert_was_accepted(self):
        code, output = self.invoke(failure=monitor.RequestFailure("HTTP 503"))
        self.assertEqual(code, 0)
        self.assert_private_values_absent(output)

    def test_email_failure_returns_nonzero(self):
        code, output = self.invoke(balance=Decimal("123.45"), mail_failure=monitor.MailFailure("private"))
        self.assertEqual(code, 1)
        self.assertNotIn("private", output)
        self.assert_private_values_absent(output)

    def test_unexpected_exception_does_not_leak(self):
        code, output = self.invoke(failure=RuntimeError(CONFIG.balance_url + CONFIG.balance_token))
        self.assertEqual(code, 1)
        self.assertNotIn("Traceback", output)
        self.assert_private_values_absent(output)

    def test_missing_configuration_does_not_leak(self):
        output = io.StringIO()
        with patch.dict(monitor.os.environ, {}, clear=True):
            with redirect_stdout(output), redirect_stderr(output):
                code = monitor.main([])
        self.assertEqual(code, 1)
        self.assert_private_values_absent(output.getvalue())


if __name__ == "__main__":
    unittest.main()
