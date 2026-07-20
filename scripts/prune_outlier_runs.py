#!/usr/bin/env python3
"""Preview or remove outlier benchmark runs that are significantly different from their peers.

Groups runs by (host, test, optimization, config_hash, result_name).  When a
group has 3 or more scored runs, any run whose ``bench_score`` is more than 50 %
away from the group median is flagged as an outlier and removed so it can be
re-run.

By default the script only reports what it would delete.  Pass ``--apply`` to
actually remove the outlier runs and their associated readings and result rows.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "benchmarks" / "power_meter.duckdb"

OUTLIER_THRESHOLD = 0.5  # 50 %


def table_names(conn: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }


def find_outlier_run_ids(
    conn: duckdb.DuckDBPyConnection,
    threshold: float = OUTLIER_THRESHOLD,
) -> list[int]:
    """Return run IDs whose bench_score is >threshold from their group median."""
    tables = table_names(conn)
    if "runs" not in tables:
        return []

    # Groups that make runs comparable.
    group_cols = ["host", "test", "optimization", "config_hash"]
    # result_name is nullable; use COALESCE so NULLs still group together.
    select_groups = ", ".join(
        f"COALESCE(r.{c}, '') AS {c}" for c in group_cols
    )
    select_groups += ", COALESCE(r.result_name, '') AS result_name"

    rows = conn.execute(
        f"""
        WITH scored AS (
            SELECT
                r.run_id,
                r.bench_score,
                {select_groups}
            FROM runs r
            WHERE r.bench_score IS NOT NULL
        ),
        medians AS (
            SELECT
                {', '.join(f"{c}" for c in group_cols)},
                result_name,
                median(bench_score) AS group_median
            FROM scored
            GROUP BY {', '.join(f"{c}" for c in group_cols)}, result_name
            HAVING count(*) >= 3
        )
        SELECT s.run_id
        FROM scored s
        JOIN medians m
            ON {' AND '.join(f"s.{c} = m.{c}" for c in group_cols)}
            AND s.result_name = m.result_name
        WHERE abs(s.bench_score - m.group_median) / m.group_median > ?
        ORDER BY s.run_id
        """,
        [threshold],
    ).fetchall()

    return [row[0] for row in rows]


def delete_runs(conn: duckdb.DuckDBPyConnection, run_ids: list[int]) -> None:
    if not run_ids:
        return
    placeholders = ", ".join("?" for _ in run_ids)
    tables = table_names(conn)
    conn.execute("BEGIN TRANSACTION")
    try:
        for table in ("readings", "run_results"):
            if table in tables:
                conn.execute(
                    f"DELETE FROM {table} WHERE run_id IN ({placeholders})", run_ids
                )
        conn.execute(f"DELETE FROM runs WHERE run_id IN ({placeholders})", run_ids)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="DuckDB database to inspect"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete the outlier runs and their child rows",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=OUTLIER_THRESHOLD,
        help=f"Max fractional deviation from median before a run is flagged (default: {OUTLIER_THRESHOLD})",
    )
    args = parser.parse_args()

    database = args.db.expanduser().resolve()
    if not database.exists():
        parser.error(f"Database not found: {database}")

    with duckdb.connect(str(database), read_only=not args.apply) as conn:
        run_ids = find_outlier_run_ids(conn, threshold=args.threshold)
        action = "Would remove" if not args.apply else "Removing"
        print(f"{action} {len(run_ids)} outlier run(s) from {database}.")
        if run_ids:
            print("Run IDs:", ", ".join(map(str, run_ids)))
        if args.apply:
            delete_runs(conn, run_ids)
            print("Deletion complete.")
        else:
            print("Re-run with --apply to delete these runs.")


if __name__ == "__main__":
    main()
