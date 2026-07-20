import asyncio
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
    def test_power_logger_keeps_every_valid_packet(self):
        stop_event = asyncio.Event()
        packets = [b"\xff\x55\x01first", b"\xff\x55\x01second"]

        class FakeConnection:
            async def read_packet(self, timeout):
                packet = packets.pop(0)
                if not packets:
                    stop_event.set()
                return packet

        readings = [MagicMock(power=10.0), MagicMock(power=11.0)]
        run = run_benchmark.RunBuffer()
        state = run_benchmark.LoggerState()
        state.checksum_policy = "strict"

        with (
            patch("run_benchmark.verify_checksum", return_value=True),
            patch("run_benchmark.parse_report", side_effect=readings),
            patch("run_benchmark.time.time", side_effect=[100.0, 100.1]),
        ):
            asyncio.run(
                run_benchmark.power_logger(
                    run, FakeConnection(), state, stop_event
                )
            )

        self.assertEqual(run.readings, [(readings[0], "settle"), (readings[1], "settle")])
        self.assertEqual(state.valid, 2)

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
