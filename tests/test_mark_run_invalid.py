import tempfile
import unittest
from pathlib import Path

import duckdb

from scripts.mark_run_invalid import mark_run_invalid, get_run_info, MIGRATION


def _make_run(conn: duckdb.DuckDBPyConnection, run_id: int = 1, test: str = "encode") -> None:
    """Insert a basic run row."""
    conn.execute(
        "INSERT INTO runs (run_id, test) VALUES (?, ?)",
        [run_id, test],
    )


class MarkRunInvalidTests(unittest.TestCase):
    def _conn(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(":memory:")

    def test_marks_run_as_invalid(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, test VARCHAR)")
            _make_run(conn)
            conn.execute(MIGRATION)

            result = mark_run_invalid(conn, 1, "manual rejection")
            self.assertTrue(result)

            info = get_run_info(conn, 1)
            self.assertEqual(info["invalid_reason"], "manual rejection")

    def test_updates_existing_invalid_reason(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, test VARCHAR)")
            _make_run(conn)
            conn.execute(MIGRATION)

            mark_run_invalid(conn, 1, "first reason")
            mark_run_invalid(conn, 1, "second reason")

            info = get_run_info(conn, 1)
            self.assertEqual(info["invalid_reason"], "second reason")

    def test_returns_false_for_nonexistent_run(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, test VARCHAR)")
            conn.execute(MIGRATION)

            result = mark_run_invalid(conn, 999, "manual rejection")
            self.assertFalse(result)

    def test_returns_false_when_runs_table_missing(self):
        with self._conn() as conn:
            result = mark_run_invalid(conn, 1, "manual rejection")
            self.assertFalse(result)

    def test_get_run_info_returns_none_for_nonexistent_run(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, test VARCHAR)")
            conn.execute(MIGRATION)

            info = get_run_info(conn, 999)
            self.assertIsNone(info)


class GetRunInfoTests(unittest.TestCase):
    def _conn(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(":memory:")

    def test_returns_run_info(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, test VARCHAR, optimization VARCHAR, "
                         "host VARCHAR, started_at TIMESTAMP)")
            conn.execute(MIGRATION)
            conn.execute(
                "INSERT INTO runs (run_id, test, optimization, host, started_at) VALUES (?, ?, ?, ?, ?)",
                [1, "encode", "baseline", "node1", "2024-01-01 12:00:00"],
            )

            info = get_run_info(conn, 1)
            self.assertIsNotNone(info)
            self.assertEqual(info["run_id"], 1)
            self.assertEqual(info["test"], "encode")
            self.assertEqual(info["optimization"], "baseline")
            self.assertEqual(info["host"], "node1")
            self.assertIsNone(info["invalid_reason"])

    def test_returns_none_when_runs_table_missing(self):
        with self._conn() as conn:
            info = get_run_info(conn, 1)
            self.assertIsNone(info)

    def test_returns_none_when_column_missing(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, test VARCHAR)")
            conn.execute(
                "INSERT INTO runs (run_id, test) VALUES (?, ?)",
                [1, "encode"],
            )

            info = get_run_info(conn, 1)
            self.assertIsNone(info)


if __name__ == "__main__":
    unittest.main()
