import tempfile
import unittest
from pathlib import Path

import duckdb

from scripts.prune_outlier_runs import find_outlier_run_ids, delete_runs


def _make_runs(conn: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    """Insert (run_id, host, test, optimization, config_hash, result_name, bench_score)."""
    conn.executemany(
        "INSERT INTO runs (run_id, host, test, optimization, config_hash, "
        "result_name, bench_score) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


class FindOutlierRunIdsTests(unittest.TestCase):
    def _conn(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(":memory:")

    def test_no_outlier_when_scores_tight(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, host VARCHAR, test VARCHAR, "
                         "optimization VARCHAR, config_hash VARCHAR, result_name VARCHAR, "
                         "bench_score DOUBLE)")
            _make_runs(conn, [
                (1, "node1", "encode", "none", "abc", "main", 100.0),
                (2, "node1", "encode", "none", "abc", "main", 102.0),
                (3, "node1", "encode", "none", "abc", "main", 101.0),
            ])
            self.assertEqual(find_outlier_run_ids(conn), [])

    def test_detects_single_outlier(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, host VARCHAR, test VARCHAR, "
                         "optimization VARCHAR, config_hash VARCHAR, result_name VARCHAR, "
                         "bench_score DOUBLE)")
            _make_runs(conn, [
                (1, "node1", "encode", "none", "abc", "main", 100.0),
                (2, "node1", "encode", "none", "abc", "main", 105.0),
                (3, "node1", "encode", "none", "abc", "main", 160.0),
            ])
            self.assertEqual(find_outlier_run_ids(conn), [3])

    def test_multiple_outliers(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, host VARCHAR, test VARCHAR, "
                         "optimization VARCHAR, config_hash VARCHAR, result_name VARCHAR, "
                         "bench_score DOUBLE)")
            _make_runs(conn, [
                (1, "node1", "encode", "none", "abc", "main", 70.0),
                (2, "node1", "encode", "none", "abc", "main", 160.0),
                (3, "node1", "encode", "none", "abc", "main", 170.0),
            ])
            # Median is 160; 70 is >50% away (56.25%).
            outliers = find_outlier_run_ids(conn)
            self.assertIn(1, outliers)

    def test_ignores_groups_with_fewer_than_three(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, host VARCHAR, test VARCHAR, "
                         "optimization VARCHAR, config_hash VARCHAR, result_name VARCHAR, "
                         "bench_score DOUBLE)")
            _make_runs(conn, [
                (1, "node1", "encode", "none", "abc", "main", 100.0),
                (2, "node1", "encode", "none", "abc", "main", 200.0),
            ])
            self.assertEqual(find_outlier_run_ids(conn), [])

    def test_null_config_hash_still_groups(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, host VARCHAR, test VARCHAR, "
                         "optimization VARCHAR, config_hash VARCHAR, result_name VARCHAR, "
                         "bench_score DOUBLE)")
            _make_runs(conn, [
                (1, "node1", "encode", "none", None, None, 100.0),
                (2, "node1", "encode", "none", None, None, 105.0),
                (3, "node1", "encode", "none", None, None, 160.0),
            ])
            self.assertEqual(find_outlier_run_ids(conn), [3])

    def test_different_groups_are_independent(self):
        with self._conn() as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER, host VARCHAR, test VARCHAR, "
                         "optimization VARCHAR, config_hash VARCHAR, result_name VARCHAR, "
                         "bench_score DOUBLE)")
            _make_runs(conn, [
                (1, "node1", "encode", "none", "abc", "main", 100.0),
                (2, "node1", "encode", "none", "abc", "main", 105.0),
                (3, "node1", "encode", "none", "abc", "main", 160.0),
                (4, "node2", "encode", "none", "abc", "main", 200.0),
                (5, "node2", "encode", "none", "abc", "main", 205.0),
                (6, "node2", "encode", "none", "abc", "main", 210.0),
            ])
            outliers = find_outlier_run_ids(conn)
            self.assertEqual(outliers, [3])


class DeleteRunsTests(unittest.TestCase):
    def test_deletes_run_and_cascades(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Path(tmpdir) / "test.duckdb"
            with duckdb.connect(str(database)) as conn:
                conn.execute("CREATE TABLE runs (run_id INTEGER)")
                conn.execute("CREATE TABLE readings (id INTEGER, run_id INTEGER)")
                conn.execute("CREATE TABLE run_results (run_id INTEGER)")
                conn.executemany("INSERT INTO runs VALUES (?)", [(1,), (2,), (3,)])
                conn.executemany("INSERT INTO readings VALUES (?, ?)", [(1, 1), (2, 1), (3, 2)])
                conn.executemany("INSERT INTO run_results VALUES (?)", [(1,), (2,)])

                delete_runs(conn, [1])
                self.assertEqual(conn.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
                self.assertEqual(conn.execute("SELECT count(*) FROM readings").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT count(*) FROM run_results").fetchone()[0], 1)

    def test_noop_on_empty_list(self):
        with duckdb.connect(":memory:") as conn:
            conn.execute("CREATE TABLE runs (run_id INTEGER)")
            delete_runs(conn, [])
            self.assertEqual(conn.execute("SELECT count(*) FROM runs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
