#!/usr/bin/env python3
"""Preview or rename a host in every matching benchmark result.

For example, to rename all results recorded for ``192.168.1.76`` to
``node2``::

    python3 scripts/rename_result_host.py 192.168.1.76 node2 --apply

Without ``--apply`` the script only reports how many rows would change.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "benchmarks" / "power_meter.duckdb"


def validate_runs_table(connection: duckdb.DuckDBPyConnection) -> None:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    if "runs" not in tables:
        raise RuntimeError("Database does not contain a runs table")

    columns = {row[1] for row in connection.execute("PRAGMA table_info('runs')").fetchall()}
    if "host" not in columns:
        raise RuntimeError("The runs table does not contain a host column")


def matching_run_count(connection: duckdb.DuckDBPyConnection, old_host: str) -> int:
    validate_runs_table(connection)
    return connection.execute(
        "SELECT count(*) FROM runs WHERE host = ?", [old_host]
    ).fetchone()[0]


def rename_host(
    connection: duckdb.DuckDBPyConnection, old_host: str, new_host: str
) -> int:
    """Rename every exact host match and return the number of changed runs."""
    if old_host == new_host:
        raise ValueError("Old and new host names must differ")

    count = matching_run_count(connection, old_host)
    if not count:
        return 0

    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            "UPDATE runs SET host = ? WHERE host = ?", [new_host, old_host]
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old_host", help="Exact IP address or host name currently stored")
    parser.add_argument("new_host", help="Replacement host name")
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help="DuckDB database to update"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Apply the rename instead of previewing it"
    )
    args = parser.parse_args()

    if args.old_host == args.new_host:
        parser.error("old_host and new_host must differ")

    database = args.db.expanduser().resolve()
    if not database.exists():
        parser.error(f"Database not found: {database}")

    try:
        with duckdb.connect(str(database), read_only=not args.apply) as connection:
            if args.apply:
                count = rename_host(connection, args.old_host, args.new_host)
                print(
                    f"Renamed {count} run(s) from {args.old_host!r} to "
                    f"{args.new_host!r} in {database}."
                )
            else:
                count = matching_run_count(connection, args.old_host)
                print(
                    f"Would rename {count} run(s) from {args.old_host!r} to "
                    f"{args.new_host!r} in {database}."
                )
                print("Re-run with --apply to update the results.")
    except (duckdb.Error, RuntimeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
