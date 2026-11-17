import json
from pathlib import Path
import tempfile
import unittest

from tools.optimization.run_baseline import read_browser_report


class ReportValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="t00-report-test-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "browser.json"

    def test_failed_browser_cannot_reuse_an_existing_report(self):
        self.path.write_text(json.dumps({"results": [{"first_data_ms": 123}], "errors": []}))
        report = read_browser_report(self.path, 1, 1)
        self.assertEqual(report["results"], [])
        self.assertTrue(report["errors"])

    def test_incomplete_success_cannot_be_reported_as_complete(self):
        self.path.write_text(json.dumps({"results": [], "errors": []}))
        report = read_browser_report(self.path, 0, 9)
        self.assertEqual(report["results"], [])
        self.assertTrue(report["errors"])

    def test_complete_current_report_is_retained(self):
        self.path.write_text(json.dumps({"results": [{"first_data_ms": 123}], "errors": []}))
        report = read_browser_report(self.path, 0, 1)
        self.assertEqual(report["results"], [{"first_data_ms": 123}])
        self.assertEqual(report["errors"], [])
