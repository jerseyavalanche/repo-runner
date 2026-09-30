import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_runner.cli import main
from repo_runner.scout import collect, save_report


class ScoutTests(unittest.TestCase):
    def test_deduplicates_and_skips_forks(self):
        item = {"full_name": "org/app", "name": "Android barcode scanner",
                "description": "offline inventory", "html_url": "https://github.com/org/app",
                "stargazers_count": 12, "pushed_at": "2026-01-01T00:00:00Z"}
        with patch("repo_runner.scout.search", return_value=[item, {**item, "fork": True}]), \
             patch("repo_runner.scout.time.sleep"):
            rows, errors = collect(["android-apps", "barcode-inventory"], delay=7)
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["categories"], ["android-apps", "barcode-inventory"])

    def test_report_and_cli_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("repo_runner.cli.collect", return_value=([], [])) as mock:
                code = main(["scout", "--category", "zebra-datawedge", "--output", directory])
            self.assertEqual(code, 0)
            self.assertEqual(mock.call_args.args[0], ["zebra-datawedge"])
            self.assertTrue((Path(directory) / "latest.md").exists())
            self.assertIn("ShopRite", (Path(directory) / "latest.md").read_text())

    def test_partial_errors_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            _, report = save_report(Path(directory), [], ["mesh-comms: rate limited"])
            self.assertIn("rate limited", report.read_text())


if __name__ == "__main__":
    unittest.main()
