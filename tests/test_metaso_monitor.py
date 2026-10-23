import json
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import patch

import metaso_monitor as monitor


class ConfigTests(unittest.TestCase):
    def test_loads_multiple_accounts_without_exposing_keys(self):
        accounts = monitor.load_accounts(raw_json=json.dumps([
            {"label": "182****5937", "key": "mk-AAAAAAAAAA"},
            {"label": "老板", "key": "mk-BBBBBBBBBB"},
        ]))
        self.assertEqual([a.label for a in accounts], ["182****5937", "老板"])
        self.assertEqual(accounts[0].key, "mk-AAAAAAAAAA")

    def test_duplicate_labels_are_rejected(self):
        with self.assertRaises(monitor.MonitorConfigError):
            monitor.load_accounts(raw_json=json.dumps([
                {"label": "same", "key": "mk-AAAAAAAAAA"},
                {"label": "same", "key": "mk-BBBBBBBBBB"},
            ]))


class ParsingTests(unittest.TestCase):
    def test_parses_scalar_usage(self):
        self.assertEqual(monitor._find_number(12), Decimal("12"))

    def test_parses_nested_remaining_value(self):
        self.assertEqual(
            monitor._find_number({"data": {"externalPackage": {"rest": 7}}}),
            Decimal("7"),
        )

    def test_rejects_boolean(self):
        self.assertIsNone(monitor._find_number(True))


class RunTests(unittest.TestCase):
    def test_reports_low_and_healthy_accounts(self):
        config = monitor.Config(
            accounts=(
                monitor.Account("low", "mk-AAAAAAAAAA"),
                monitor.Account("ok", "mk-BBBBBBBBBB"),
            ),
            threshold=Decimal("0"),
        )
        with patch.object(monitor, "request_value", side_effect=[0, 3]):
            report = monitor.run(config)
        self.assertTrue(report["alert"])
        self.assertEqual([r["status"] for r in report["accounts"]], ["low", "healthy"])

    def test_report_file_is_written_with_restricted_permissions(self):
        config = monitor.Config(accounts=(monitor.Account("ok", "mk-AAAAAAAAAA"),))
        with patch.object(monitor, "request_value", return_value=1):
            report = monitor.run(config)
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/report.json"
            monitor.write_report(report, path)
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            self.assertFalse(loaded["alert"])


if __name__ == "__main__":
    unittest.main()
