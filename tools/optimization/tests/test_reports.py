import json
from pathlib import Path
import tempfile
import unittest

from tools.optimization.run_baseline import read_browser_report, start_run


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

    def test_wrong_run_cannot_satisfy_current_sample_count(self):
        self.path.write_text(json.dumps({"run_id": "old", "status": "completed",
                                        "results": [{}], "errors": []}))
        report = read_browser_report(self.path, 0, 1, run_id="current")
        self.assertTrue(report["errors"])

    def test_running_checkpoint_cannot_be_accepted(self):
        self.path.write_text(json.dumps({"run_id": "current", "status": "running",
                                        "results": [{}], "errors": []}))
        report = read_browser_report(self.path, 0, 1, run_id="current")
        self.assertTrue(report["errors"])

    def test_existing_output_is_refused_without_overwriting(self):
        self.path.write_text("old evidence")
        with self.assertRaises(FileExistsError):
            start_run(self.path.parent)
        self.assertEqual(self.path.read_text(), "old evidence")
