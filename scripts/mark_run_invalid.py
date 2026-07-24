#!/usr/bin/env python3
"""Mark a specific run as invalid with a reason.

This script adds or updates the invalid_reason column for a given run_id.
When invalid_reason is set, the run is considered invalid and will be
excluded from analysis and reports.

Usage:
    python mark_run_invalid.py --run-id 42 --reason "manual rejection"
    python mark_run_invalid.py --run-id 42 --reason "manual rejection" --db benchmarks/power_meter.duckdb
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "benchmarks" / "power_meter.duckdb"

MIGRATION = "ALTER TABLE runs ADD COLUMN IF NOT EXISTS invalid_reason VARCHAR"


def table_names(connection: duckdb.DuckDBPyConnection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }


def run_exists(connection: duckdb.DuckDBPyConnection, run_id: int) -> bool:
    """Check if a run with the given ID exists."""
    result = connection.execute(
        "SELECT 1 FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    return result is not None


def mark_run_invalid(connection: duckdb.DuckDBPyConnection, run_id: int, reason: str) -> bool:
    """Mark a run as invalid with the given reason.

    Returns True if the run was successfully marked, False otherwise.
    """
    tables = table_names(connection)
    if "runs" not in tables:
        print("Error: 'runs' table does not exist.", file=sys.stderr)
        return False

    # Ensure the column exists
    connection.execute(MIGRATION)

    # Check if run exists
    if not run_exists(connection, run_id):
        print(f"Error: Run {run_id} not found.", file=sys.stderr)
        return False

    # Mark the run as invalid
    connection.execute(
        "UPDATE runs SET invalid_reason = ? WHERE run_id = ?",
        [reason, run_id],
    )
    return True


def get_run_info(connection: duckdb.DuckDBPyConnection, run_id: int) -> dict | None:
    """Get basic information about a run."""
    tables = table_names(connection)
    if "runs" not in tables:
        return None

    columns = {row[1] for row in connection.execute("PRAGMA table_info('runs')").fetchall()}
    if "invalid_reason" not in columns:
        return None

    # Build a SELECT that only references columns that exist
    available_columns = ["run_id", "test", "invalid_reason"]
    optional_columns = ["optimization", "host", "started_at"]
    for col in optional_columns:
        if col in columns:
            available_columns.append(col)

    select_clause = ", ".join(available_columns)
    result = connection.execute(
        f"SELECT {select_clause} FROM runs WHERE run_id = ?",
        [run_id],
    ).fetchone()
    if not result:
        return None

    # Build the result dict from the available columns
    info = {"run_id": result[0], "test": result[1], "invalid_reason": result[2]}
    idx = 3
    for col in optional_columns:
        if col in columns:
            info[col] = result[idx]
            idx += 1
    return info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", type=int, required=True,
        help="The run_id to mark as invalid",
    )
    parser.add_argument(
        "--reason", type=str, default="manual rejection",
        help="Reason for marking the run as invalid (default: 'manual rejection')",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help="DuckDB database to modify",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without making changes",
    )
    args = parser.parse_args()

    database = args.db.expanduser().resolve()
    if not database.exists():
        parser.error(f"Database not found: {database}")

    with duckdb.connect(str(database), read_only=args.dry_run) as conn:
        # First, check if the run exists and show current status
        run_info = get_run_info(conn, args.run_id)
        if not run_info:
            print(f"Error: Run {args.run_id} not found or invalid_reason column missing.", file=sys.stderr)
            sys.exit(1)

        print(f"Run #{args.run_id}:")
        print(f"  Test: {run_info['test']}")
        if "optimization" in run_info:
            print(f"  Optimization: {run_info['optimization']}")
        if "host" in run_info:
            print(f"  Host: {run_info['host']}")
        if "started_at" in run_info:
            print(f"  Started: {run_info['started_at']}")
        print(f"  Current invalid_reason: {run_info['invalid_reason'] or '(valid)'}")

        if args.dry_run:
            print(f"\nDry run: Would mark run #{args.run_id} as invalid with reason: '{args.reason}'")
            return

        # Mark the run as invalid
        if mark_run_invalid(conn, args.run_id, args.reason):
            print(f"\nSuccessfully marked run #{args.run_id} as invalid with reason: '{args.reason}'")
            # Show updated status
            updated_info = get_run_info(conn, args.run_id)
            if updated_info:
                print(f"  Updated invalid_reason: {updated_info['invalid_reason']}")
        else:
            print(f"\nFailed to mark run #{args.run_id} as invalid.", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
