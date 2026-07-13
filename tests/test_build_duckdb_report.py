import json
from html.parser import HTMLParser
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import duckdb

from scripts.build_duckdb_report import render_report


class _DocumentParser(HTMLParser):
    pass


class DuckDbReportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = self.root / "fixture.duckdb"
        self.output = self.root / "report.html"

    def tearDown(self):
        self.tempdir.cleanup()

    def create_fixture(self):
        with duckdb.connect(str(self.database)) as connection:
            connection.execute(
                """
                CREATE TABLE runs (
                    run_id INTEGER,
                    started_at TIMESTAMP,
                    host VARCHAR,
                    test VARCHAR,
                    optimization VARCHAR,
                    repeat_idx INTEGER,
                    applied_config VARCHAR,
                    bench_end DOUBLE,
                    bench_score DOUBLE,
                    bench_unit VARCHAR,
                    higher_is_better BOOLEAN,
                    dropped_packets INTEGER,
                    checksum_failures INTEGER,
                    bench_sample_coverage DOUBLE,
                    energy_wh_integrated DOUBLE
                )
                """
            )
            connection.execute(
                "CREATE TABLE readings (run_id INTEGER, phase VARCHAR, power_w DOUBLE, timestamp DOUBLE)"
            )
            connection.execute(
                "CREATE TABLE run_results (run_id INTEGER, title VARCHAR, value DOUBLE)"
            )

            rows = [
                (1, "alpha", "compile", "baseline", 100.0, 10.0, 70.0, False),
                (2, "alpha", "compile", "eco", 102.0, 7.0, 52.0, False),
                (3, "alpha", "idle", "baseline", None, None, 5.0, False),
                (4, "alpha", "idle", "eco", None, None, 4.0, False),
                (5, "beta", "compile", "baseline", 200.0, 20.0, 110.0, False),
                (6, "beta", "compile", "eco", 198.0, 15.0, 87.0, False),
                (7, "beta", "idle", "baseline", None, None, 7.0, False),
                (8, "beta", "idle", "eco", None, None, 5.5, False),
            ]
            for run_id, host, test, optimization, score, energy, power, higher in rows:
                connection.execute(
                    """
                    INSERT INTO runs VALUES (
                        ?, TIMESTAMP '2026-07-12 12:00:00', ?, ?, ?, 1, '{}',
                        2.0, ?, 'seconds', ?, 0, 0, 0.98, ?
                    )
                    """,
                    [run_id, host, test, optimization, score, higher, energy],
                )
                phase = "idle" if test == "idle" else "bench"
                connection.executemany(
                    "INSERT INTO readings VALUES (?, ?, ?, ?)",
                    [(run_id, phase, power, run_id * 10.0), (run_id, phase, power, run_id * 10.0 + 1.0)],
                )

            connection.execute(
                """
                INSERT INTO runs VALUES (
                    9, TIMESTAMP '2026-07-12 13:00:00', 'beta', 'compile',
                    'unsafe', 1, ?, NULL, 123.0, 'seconds', false, 0, 0, 0.2, 9.0
                )
                """,
                ['{"note":"</script><script>alert(1)</script>"}'],
            )
            connection.execute("ALTER TABLE runs ADD COLUMN kernel VARCHAR")
            connection.execute("ALTER TABLE runs ADD COLUMN cpu_model VARCHAR")
            connection.execute("ALTER TABLE runs ADD COLUMN memory_bytes BIGINT")
            connection.execute(
                "UPDATE runs SET kernel = host || '-kernel', cpu_model = host || '-cpu', "
                "memory_bytes = CASE host WHEN 'alpha' THEN 17179869184 ELSE 34359738368 END"
            )

    def embedded_payload(self, report):
        match = re.search(
            r'<script id="reportData" type="application/json">(.*?)</script>',
            report,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def test_multi_host_report_contains_decision_views_and_safe_data(self):
        self.create_fixture()
        render_report(self.database, self.output, row_limit=2)
        report = self.output.read_text(encoding="utf-8")
        payload = self.embedded_payload(report)

        self.assertEqual(payload["meta"]["hosts"], ["alpha", "beta"])
        self.assertEqual(payload["meta"]["validRunCount"], 8)
        self.assertEqual(payload["meta"]["runCount"], 9)
        self.assertEqual(
            next(run for run in payload["runs"] if run["run_id"] == 9)["valid"],
            False,
        )
        self.assertIn('id="rankChart"', report)
        self.assertIn('id="scatter"', report)
        self.assertIn('id="heatmap"', report)
        self.assertIn('id="hostFilter"', report)
        self.assertIn('id="metricFilter"', report)
        self.assertIn("function metric(", report)
        self.assertIn("function renderRuns(", report)
        self.assertIn('id="hostConfigs"', report)
        self.assertIn('id="coverageView"', report)
        self.assertIn('id="coverageGrid"', report)
        self.assertIn("function renderCoverage(", report)
        self.assertIn("DATA.runs.length+' recorded runs'", report)
        self.assertIn("r.host,r.test,r.optimization", report)
        self.assertIn("counts.get([h,t,n]", report)
        self.assertIn("<th>Configuration</th>", report)
        self.assertIn('class="coverage-test"', report)
        self.assertIn('runs/2.html', report)
        host_specs = {
            host["host"]: {spec["label"]: spec["values"] for spec in host["specs"]}
            for host in payload["hostConfigs"]
        }
        self.assertEqual(host_specs["alpha"]["CPU model"], ["alpha-cpu"])
        self.assertEqual(host_specs["beta"]["Memory"], [34359738368])

        detail = self.root / "runs" / "2.html"
        self.assertTrue(detail.is_file())
        detail_report = detail.read_text(encoding="utf-8")
        detail_match = re.search(
            r'<script id="runData" type="application/json">(.*?)</script>',
            detail_report,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(detail_match)
        detail_payload = json.loads(detail_match.group(1))
        self.assertEqual(detail_payload["run"]["run_id"], 2)
        self.assertEqual([point["elapsed_s"] for point in detail_payload["readings"]], [0.0, 1.0])
        self.assertIn("Power consumption over time", detail_report)
        self.assertNotIn("</script><script>alert(1)</script>", report)
        self.assertIn(r"\u003c/script\u003e", report)

        parser = _DocumentParser()
        parser.feed(report)
        parser.close()

        if shutil.which("node"):
            scripts = re.findall(r"<script(?: [^>]*)?>(.*?)</script>", report, re.DOTALL)
            result = subprocess.run(
                ["node", "--check"],
                input=scripts[-1],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            detail_scripts = re.findall(
                r"<script(?: [^>]*)?>(.*?)</script>", detail_report, re.DOTALL
            )
            result = subprocess.run(
                ["node", "--check"],
                input=detail_scripts[-1],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_empty_database_still_renders_a_useful_shell(self):
        with duckdb.connect(str(self.database)):
            pass
        render_report(self.database, self.output, row_limit=1)
        report = self.output.read_text(encoding="utf-8")
        payload = self.embedded_payload(report)
        self.assertEqual(payload["meta"]["runCount"], 0)
        self.assertEqual(payload["tables"], [])
        self.assertIn("No tables found.", report)


if __name__ == "__main__":
    unittest.main()
