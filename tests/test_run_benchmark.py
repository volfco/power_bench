import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import run_benchmark


def benchmark_args(**overrides):
    values = {
        "host": "node2",
        "connection_host": "192.0.2.10",
        "connection_user": "benchmark",
        "connection_key": None,
        "db": "unused.duckdb",
        "test": "idle",
        "optimization": "baseline",
        "repeat": 1,
        "config_hash": "abc123",
        "ambient": None,
        "idle_only": True,
        "reboot": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RunBufferTests(unittest.TestCase):
    def test_failed_run_never_opens_duckdb(self):
        args = benchmark_args()
        with (
            patch("run_benchmark.gather_host_info", return_value={}),
            patch("run_benchmark.run_async", new_callable=AsyncMock, side_effect=RuntimeError("meter failed")),
            patch("run_benchmark.Database") as database,
        ):
            with self.assertRaisesRegex(SystemExit, "2"):
                run_benchmark.run(args)

        database.assert_not_called()

    def test_completed_run_opens_duckdb_once_and_persists_once(self):
        args = benchmark_args()
        database = MagicMock()
        database.persist_run.return_value = 7
        database_context = MagicMock()
        database_context.__enter__.return_value = database

        with (
            patch("run_benchmark.gather_host_info", return_value={}),
            patch("run_benchmark.run_async", new_callable=AsyncMock, return_value=0),
            patch("run_benchmark.Database", return_value=database_context) as database_class,
            patch("run_benchmark.print_summary"),
        ):
            with self.assertRaisesRegex(SystemExit, "0"):
                run_benchmark.run(args)

        database_class.assert_called_once_with("unused.duckdb")
        database.persist_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
