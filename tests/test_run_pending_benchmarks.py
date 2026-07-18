import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import run_pending_benchmarks


class RunPendingBenchmarksTests(unittest.TestCase):
    @patch("run_pending_benchmarks.resolve_inventory_host")
    @patch("run_pending_benchmarks.subprocess.run")
    def test_uses_three_repeat_resume_mode(self, run, resolve_host):
        run.return_value = SimpleNamespace(returncode=0)

        result = run_pending_benchmarks.main(["node2", "--dry-run"])

        self.assertEqual(result, 0)
        resolve_host.assert_called_once()
        command = run.call_args.args[0]
        self.assertEqual(
            command[:6],
            [
                sys.executable,
                str(run_pending_benchmarks.ROOT / "run_suite.py"),
                "node2",
                "--repeats",
                "3",
                "--skip-existing",
            ],
        )
        self.assertEqual(command[-1], "--dry-run")

    @patch("run_pending_benchmarks.resolve_inventory_host")
    @patch("run_pending_benchmarks.subprocess.run")
    def test_explicit_sweep_is_preserved(self, run, _resolve_host):
        run.return_value = SimpleNamespace(returncode=0)

        run_pending_benchmarks.main(["node2", "--sweep", "amd", "--list"])

        command = run.call_args.args[0]
        self.assertEqual(command.count("--sweep"), 1)
        self.assertEqual(command[command.index("--sweep") + 1], "amd")

    def test_rejects_options_that_break_the_three_repeat_resume_contract(self):
        with self.assertRaises(SystemExit):
            run_pending_benchmarks.main(["node2", "--repeats", "2"])


if __name__ == "__main__":
    unittest.main()
