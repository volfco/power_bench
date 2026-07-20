#!/usr/bin/env python3
"""Preview or remove incomplete and invalid power-benchmark runs.

By default this reports the runs that would be removed.  Pass ``--apply`` to
delete them and their associated readings and result rows.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "benchmarks" / "power_meter.duckdb"


def table_names(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }


def run_columns(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {row[1] for row in connection.execute("PRAGMA table_info('runs')").fetchall()}


def invalid_run_ids(connection: duckdb.DuckDBPyConnection) -> list[int]:
    """Return run IDs that do not meet the suite/dashboard validity criteria."""
    tables = table_names(connection)
    if "runs" not in tables:
        return []
    columns = run_columns(connection)
    dropped = "COALESCE(r.dropped_packets, 0) > 0" if "dropped_packets" in columns else "FALSE"
    complete = "r.bench_end IS NOT NULL" if "bench_end" in columns else "FALSE"
    scored = "r.bench_score IS NOT NULL" if "bench_score" in columns else "FALSE"
    coverage = "COALESCE(r.bench_sample_coverage, 0) >= 0.8" if "bench_sample_coverage" in columns else "FALSE"
    readings = (
        "SELECT run_id, count(*) FILTER (WHERE phase = 'idle') AS idle_samples "
        "FROM readings GROUP BY run_id"
        if "readings" in tables
        else "SELECT CAST(NULL AS INTEGER) AS run_id, CAST(0 AS BIGINT) AS idle_samples WHERE FALSE"
    )
    rows = connection.execute(
        f"""
        WITH run_readings AS ({readings})
        SELECT r.run_id
        FROM runs r
        LEFT JOIN run_readings rr ON rr.run_id = r.run_id
        WHERE CASE
            WHEN COALESCE(r.test, '') = 'idle'
                THEN COALESCE(rr.idle_samples, 0) = 0 OR {dropped}
            ELSE NOT ({complete} AND {scored} AND {coverage}) OR {dropped}
        END
        ORDER BY r.run_id
        """
    ).fetchall()
    return [row[0] for row in rows]


def delete_runs(connection: duckdb.DuckDBPyConnection, run_ids: list[int]) -> None:
    if not run_ids:
        return
    placeholders = ", ".join("?" for _ in run_ids)
    tables = table_names(connection)
    connection.execute("BEGIN TRANSACTION")
    try:
        for table in ("readings", "run_results"):
            if table in tables:
                connection.execute(f"DELETE FROM {table} WHERE run_id IN ({placeholders})", run_ids)
        connection.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", run_ids)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="DuckDB database to inspect")
    parser.add_argument("--apply", action="store_true", help="Delete the identified runs and their child rows")
    args = parser.parse_args()
    database = args.db.expanduser().resolve()
    if not database.exists():
        parser.error(f"Database not found: {database}")

    with duckdb.connect(str(database), read_only=not args.apply) as connection:
        run_ids = invalid_run_ids(connection)
        action = "Would remove" if not args.apply else "Removing"
        print(f"{action} {len(run_ids)} incomplete or invalid run(s) from {database}.")
        if run_ids:
            print("Run IDs:", ", ".join(map(str, run_ids)))
        if args.apply:
            delete_runs(connection, run_ids)
            print("Deletion complete.")
        else:
            print("Re-run with --apply to delete these runs.")


if __name__ == "__main__":
    main()
