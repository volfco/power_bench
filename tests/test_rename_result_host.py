import tempfile
import unittest
from pathlib import Path

import duckdb

from scripts.rename_result_host import matching_run_count, rename_host


class RenameResultHostTests(unittest.TestCase):
    def test_rename_host_updates_every_exact_match(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            database = Path(tmpdir) / "results.duckdb"
            with duckdb.connect(str(database)) as connection:
                connection.execute("CREATE TABLE runs (run_id INTEGER, host VARCHAR)")
                connection.executemany(
                    "INSERT INTO runs VALUES (?, ?)",
                    [(1, "192.168.1.76"), (2, "node1"), (3, "192.168.1.76")],
                )

                self.assertEqual(matching_run_count(connection, "192.168.1.76"), 2)
                self.assertEqual(rename_host(connection, "192.168.1.76", "node2"), 2)
                self.assertEqual(
                    connection.execute(
                        "SELECT run_id, host FROM runs ORDER BY run_id"
                    ).fetchall(),
                    [(1, "node2"), (2, "node1"), (3, "node2")],
                )

    def test_rename_host_rejects_identical_names(self):
        with duckdb.connect(":memory:") as connection:
            connection.execute("CREATE TABLE runs (run_id INTEGER, host VARCHAR)")
            with self.assertRaisesRegex(ValueError, "must differ"):
                rename_host(connection, "node2", "node2")


if __name__ == "__main__":
    unittest.main()
