"""Offline tests for the balance check core (no network, no secrets)."""
import json
import unittest
from decimal import Decimal
from typing import Any

import balance_check as bc
import worker_app as wa


def accounts_env() -> str:
    return json.dumps([
        {"name": "账号A", "key": "mk-aaa", "threshold": 1500},
        {"name": "账号B", "key": "mk-bbb", "threshold": "20.5", "operator": "lt", "path": "data.items[0].credits"},
    ])


class ParsingTests(unittest.TestCase):
    def test_parses_accounts_and_defaults(self):
        parsed = bc.parse_accounts(json.loads(accounts_env()))
        self.assertEqual([a.name for a in parsed], ["账号A", "账号B"])
        self.assertEqual(parsed[0].operator, "gte")
        self.assertEqual(parsed[0].value_path, "data.balance")
        self.assertEqual(parsed[1].threshold, Decimal("20.5"))
        self.assertIsNone(parsed[0].threshold_to)

    def test_rejects_bad_documents(self):
        for bad in ([], {}, [{"name": "a"}], [{"name": "a", "key": "x"}], [{"name": "a", "key": "mk-1", "threshold": "NaN"}]):
            with self.assertRaises(bc.ConfigError):
                bc.parse_accounts(bad)

    def test_rejects_duplicate_names_and_between_without_upper_bound(self):
        with self.assertRaises(bc.ConfigError):
            bc.parse_accounts([{"name": "a", "key": "mk-1"}, {"name": "a", "key": "mk-2"}])
        with self.assertRaises(bc.ConfigError):
            bc.parse_accounts([{"name": "a", "key": "mk-1", "operator": "between"}])
        ok = bc.parse_accounts([{"name": "a", "key": "mk-1", "operator": "between", "threshold": 1, "thresholdTo": 5}])
        self.assertEqual(ok[0].threshold_to, Decimal(5))

    def test_api_url_requires_https(self):
        self.assertEqual(bc.api_url(None), bc.DEFAULT_URL)
        with self.assertRaises(bc.ConfigError):
            bc.api_url("http://insecure.example")


class ExtractionTests(unittest.TestCase):
    def test_field_at_supports_paths(self):
        doc = {"data": {"items": [{"credits": 7}], "balance": 3}}
        self.assertEqual(bc.field_at(doc, "data.items[0].credits"), 7)
        self.assertEqual(bc.field_at(doc, "data.balance"), 3)
        self.assertIsNone(bc.field_at(doc, "data.missing"))

    def test_balance_falls_back_to_known_field_names(self):
        self.assertEqual(bc.balance_at({"data": {"remaining": "42"}}, "data.balance"), Decimal(42))
        self.assertEqual(bc.balance_at({"externalPackage": {"credits": 9}}, "data.x"), Decimal(9))
        self.assertIsNone(bc.balance_at({"data": {"note": "x"}}, "data.balance"))
        self.assertIsNone(bc.balance_at({"data": {"balance": True}}, "data.balance"))

    def test_satisfies_every_operator(self):
        base = {"name": "a", "key": "mk-1", "threshold": Decimal(10)}
        cases = [
            ("lt", 9, True), ("lt", 10, False), ("lte", 10, True), ("gt", 11, True),
            ("gt", 10, False), ("gte", 10, True), ("eq", 10, True), ("ne", 10, False),
        ]
        for operator, value, expected in cases:
            account = bc.Account(operator=operator, **base)
            self.assertEqual(bc.satisfies(Decimal(value), account), expected, (operator, value))
        between = bc.Account(operator="between", threshold_to=Decimal(20), **base)
        self.assertTrue(bc.satisfies(Decimal(10), between))
        self.assertFalse(bc.satisfies(Decimal(21), between))


class CheckTests(unittest.TestCase):
    def account(self, **kw):
        return bc.Account(name="账号A", key="mk-aaa", threshold=Decimal(1500), **kw)

    def test_healthy_account(self):
        result = bc.check_account(self.account(), bc.DEFAULT_URL, fetch=lambda url, key: {"data": {"balance": Decimal("2048.5")}})
        self.assertTrue(result.ok)
        self.assertEqual(result.value, Decimal("2048.5"))
        self.assertIsNone(result.error)

    def test_low_balance_reports_numbers(self):
        result = bc.check_account(self.account(), bc.DEFAULT_URL, fetch=lambda url, key: {"data": {"balance": 320}})
        self.assertFalse(result.ok)
        body = result.as_json()
        self.assertEqual(body["value"], "320")
        self.assertEqual(body["threshold"], "1500")
        self.assertIn("阈值未满足", body["error"])

    def test_network_failure_is_an_error_not_a_crash(self):
        def boom(url, key):
            raise RuntimeError("HTTP 401")
        result = bc.check_account(self.account(), bc.DEFAULT_URL, fetch=boom)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "HTTP 401")

    def test_missing_value_is_reported(self):
        result = bc.check_account(self.account(), bc.DEFAULT_URL, fetch=lambda url, key: {"data": {}})
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "no numeric balance")


class _AttrEnv(dict):
    """Workers bindings are object attributes, not mapping keys."""

    def __getattr__(self, name):
        return self.get(name, "")


class HandlerTests(unittest.TestCase):
    def env(self, **overrides):
        values = {"CHECK_TOKEN": "t0ken", "ACCOUNTS_JSON": accounts_env()}
        values.update(overrides)
        return _AttrEnv(values)

    def call(self, path, env, auth="Bearer t0ken", method="GET"):
        return wa.handler(wa.parse_request(path, method, auth), env)

    def test_health(self):
        status, _, body = self.call("/health", self.env())
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_unauthenticated_and_unconfigured(self):
        self.assertEqual(self.call("/check/账号A", self.env(), auth="Bearer wrong")[0], 401)
        self.assertEqual(self.call("/check/账号A", self.env(CHECK_TOKEN=""))[0], 503)

    def test_unknown_account_lists_names(self):
        status, _, body = self.call("/check/nobody", self.env())
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["accounts"], ["账号A", "账号B"])

    def test_bad_config_fails_closed(self):
        self.assertEqual(self.call("/check/账号A", self.env(ACCOUNTS_JSON="{"))[0], 500)

    def test_recovered_flag_is_reported_once(self):
        recorded = []
        healthy = bc.CheckResult("账号A", True, Decimal(2000), Decimal(1500), None, None)
        original = wa.check_account
        try:
            wa.check_account = lambda account, url: healthy
            _, _, body = wa.handler(wa.parse_request("/check/账号A", "GET", "Bearer t0ken"), self.env(), "failed", recorded.append)
            self.assertTrue(json.loads(body)["recovered"])
            self.assertEqual(recorded, ["ok"])
            _, _, body = wa.handler(wa.parse_request("/check/账号A", "GET", "Bearer t0ken"), self.env(), "ok", recorded.append)
            self.assertNotIn("recovered", json.loads(body))
            _, _, body = wa.handler(wa.parse_request("/check/账号A", "GET", "Bearer t0ken"), self.env(), None, recorded.append)
            self.assertNotIn("recovered", json.loads(body))
            self.assertEqual(recorded, ["ok", "ok"])
        finally:
            wa.check_account = original

    def test_rejects_non_get(self):
        self.assertEqual(self.call("/check/账号A", self.env(), method="POST")[0], 405)

    def test_check_outcome_maps_to_status(self):
        # Executed normally the check would hit the network; swap the worker's binding.
        original = wa.check_account
        try:
            wa.check_account = lambda account, url: bc.CheckResult("账号A", True, Decimal(2000), Decimal(1500), None, None)
            status, _, body = self.call("/check/账号A", self.env())
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"account": "账号A", "ok": True, "value": "2000", "threshold": "1500", "rule": "data.balance gte 1500"})
            wa.check_account = lambda account, url: bc.CheckResult("账号A", False, Decimal(10), Decimal(1500), "阈值未满足", None)
            status, _, body = self.call("/check/账号A", self.env())
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(body)["value"], "10")
        finally:
            wa.check_account = original


class SummaryTests(unittest.TestCase):
    def test_digest_layout(self):
        rows = [
            bc.CheckResult("账号A", True, Decimal(2000), Decimal(1500), None, None),
            bc.CheckResult("账号B", False, Decimal(5), Decimal(20), "阈值未满足", None),
        ]
        text: Any = bc.summary(rows)
        self.assertIn("账号A：2000", text)
        self.assertIn("账号B：5", text)


if __name__ == "__main__":
    unittest.main()
