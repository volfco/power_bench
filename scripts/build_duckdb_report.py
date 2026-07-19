#!/usr/bin/env python3
"""Render a DuckDB benchmark database as a standalone interactive HTML report."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
from decimal import Decimal
from html import escape
import json
import math
from pathlib import Path
from typing import Any

import duckdb


DEFAULT_ROW_LIMIT = 250
IDLE_TEST = "idle"
TEMPLATE = Path(__file__).with_name("report_template.html")
RUN_TEMPLATE = Path(__file__).with_name("run_report_template.html")
HOST_SPEC_FIELDS = (
    ("cpu_model", "CPU model"),
    ("memory_bytes", "Memory"),
    ("kernel", "Kernel"),
    ("scaling_driver", "CPU frequency driver"),
    ("epp", "Energy performance preference"),
    ("aspm_policy", "PCIe ASPM policy"),
    ("io_scheduler", "I/O scheduler"),
    ("cmdline", "Kernel command line"),
)
READING_FIELDS = (
    "timestamp",
    "capture_time",
    "phase",
    "power_w",
    "voltage_v",
    "current_a",
    "energy_wh",
    "temperature_c",
    "power_factor",
)
RUN_FIELDS = (
    "run_id",
    "started_at",
    "host",
    "test",
    "optimization",
    "repeat_idx",
    "config_hash",
    "kernel",
    "cpu_model",
    "memory_bytes",
    "governor",
    "turbo",
    "ambient_c",
    "applied_config",
    "bench_start_temp_c",
    "result_name",
    "bench_start",
    "bench_end",
    "bench_score",
    "bench_unit",
    "higher_is_better",
    "dropped_packets",
    "checksum_failures",
    "bench_sample_coverage",
)


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def json_value(value: Any) -> Any:
    """Return a JSON-safe, human-meaningful representation of a DuckDB value."""
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return f"{len(value)} bytes"
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return str(value)


def json_mapping(value: Any) -> dict[str, Any]:
    """Return an applied-configuration JSON object, or an empty mapping."""
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def host_config_payload(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Summarize observed hardware and OS configuration for every benchmark host."""
    by_host: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        by_host.setdefault(str(run["host"]), []).append(run)

    hosts: list[dict[str, Any]] = []
    for host, host_runs in sorted(by_host.items()):
        specs = []
        for field, label in HOST_SPEC_FIELDS:
            values: list[Any] = []
            for run in host_runs:
                value = run.get(field)
                if not present(value):
                    value = json_mapping(run.get("applied_config")).get(field)
                if present(value) and value not in values:
                    values.append(value)
            specs.append({"label": label, "values": values})
        hosts.append(
            {
                "host": host,
                "runCount": len(host_runs),
                "specs": specs,
            }
        )
    return hosts


def reading_payload(
    connection: duckdb.DuckDBPyConnection, tables: set[str], run_id: int
) -> list[dict[str, Any]]:
    """Return an ordered, chart-ready power history for one run."""
    if "readings" not in tables:
        return []
    columns = set(table_columns(connection, "readings"))
    if not {"run_id", "power_w"}.issubset(columns):
        return []

    selections = [
        quote_identifier(field)
        if field in columns
        else f"NULL AS {quote_identifier(field)}"
        for field in READING_FIELDS
    ]
    order = (
        f"{quote_identifier('timestamp')} ASC NULLS LAST"
        if "timestamp" in columns
        else f"{quote_identifier('id')} ASC"
        if "id" in columns
        else "1"
    )
    readings = rows_as_dicts(
        connection.execute(
            f"SELECT {', '.join(selections)} FROM readings "
            f"WHERE {quote_identifier('run_id')} = ? ORDER BY {order}",
            [run_id],
        )
    )
    timestamps = [
        float(reading["timestamp"])
        for reading in readings
        if isinstance(reading.get("timestamp"), (int, float))
        and math.isfinite(float(reading["timestamp"]))
    ]
    origin = timestamps[0] if timestamps else None
    for index, reading in enumerate(readings):
        timestamp = reading.get("timestamp")
        if origin is not None and isinstance(timestamp, (int, float)):
            reading["elapsed_s"] = float(timestamp) - origin
        else:
            reading["elapsed_s"] = float(index)
    return readings


def run_detail_payload(
    connection: duckdb.DuckDBPyConnection,
    tables: set[str],
    run: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run": run,
        "config": json_mapping(run.get("applied_config")),
        "readings": reading_payload(connection, tables, int(run["run_id"])),
        "results": run_result_payload(connection, tables, int(run["run_id"])),
    }


def run_result_payload(
    connection: duckdb.DuckDBPyConnection, tables: set[str], run_id: int
) -> list[dict[str, Any]]:
    """Return every benchmark sub-test recorded for a run."""
    if "run_results" not in tables:
        return []
    columns = set(table_columns(connection, "run_results"))
    required = {"run_id", "title", "value"}
    if not required.issubset(columns):
        return []
    selections = [
        quote_identifier(field)
        if field in columns
        else f"NULL AS {quote_identifier(field)}"
        for field in ("title", "scale", "higher_is_better", "value")
    ]
    return rows_as_dicts(
        connection.execute(
            f"SELECT {', '.join(selections)} FROM run_results "
            f"WHERE {quote_identifier('run_id')} = ? ORDER BY title, scale",
            [run_id],
        )
    )


def detail_filename(run_id: Any) -> str | None:
    try:
        return f"{int(run_id)}.html"
    except (TypeError, ValueError):
        return None


def table_names(connection: duckdb.DuckDBPyConnection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
    ]


def table_columns(connection: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    return [
        row[1]
        for row in connection.execute(
            f"PRAGMA table_info({quote_identifier(table)})"
        ).fetchall()
    ]


def rows_as_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [
        {column: json_value(value) for column, value in zip(columns, row)}
        for row in cursor.fetchall()
    ]


def energy_expression(columns: set[str]) -> str:
    candidates: list[str] = []
    if "energy_wh_integrated" in columns:
        candidates.append("r.energy_wh_integrated")
    if {"energy_wh_bench_start", "energy_wh_bench_end"}.issubset(columns):
        candidates.append("r.energy_wh_bench_end - r.energy_wh_bench_start")
    if not candidates:
        return "CAST(NULL AS DOUBLE)"
    if len(candidates) == 1:
        return candidates[0]
    return f"coalesce({', '.join(candidates)})"


def run_payload(
    connection: duckdb.DuckDBPyConnection, tables: set[str]
) -> list[dict[str, Any]]:
    """Return one compact analytical row per run, including phase power summaries."""
    if "runs" not in tables:
        return []

    columns = set(table_columns(connection, "runs"))
    selections = [
        f"r.{quote_identifier(field)} AS {quote_identifier(field)}"
        if field in columns
        else f"NULL AS {quote_identifier(field)}"
        for field in RUN_FIELDS
    ]
    selections.append(f"{energy_expression(columns)} AS energy_wh")

    reading_columns = (
        set(table_columns(connection, "readings")) if "readings" in tables else set()
    )
    has_power_stats = {"run_id", "phase", "power_w"}.issubset(reading_columns)
    if has_power_stats:
        selections.extend(
            (
                "coalesce(s.idle_samples, 0) AS idle_samples",
                "coalesce(s.bench_samples, 0) AS bench_samples",
                "s.idle_power_w",
                "s.bench_power_w",
                "s.peak_power_w",
            )
        )
        sample_stats = """
            WITH sample_stats AS (
                SELECT run_id,
                       count(*) FILTER (WHERE phase = 'idle') AS idle_samples,
                       count(*) FILTER (WHERE phase = 'bench') AS bench_samples,
                       avg(power_w) FILTER (WHERE phase = 'idle') AS idle_power_w,
                       avg(power_w) FILTER (WHERE phase = 'bench') AS bench_power_w,
                       max(power_w) FILTER (WHERE phase = 'bench') AS peak_power_w
                FROM readings
                GROUP BY run_id
            )
        """
        join = "LEFT JOIN sample_stats s ON s.run_id = r.run_id"
    else:
        selections.extend(
            (
                "0 AS idle_samples",
                "0 AS bench_samples",
                "CAST(NULL AS DOUBLE) AS idle_power_w",
                "CAST(NULL AS DOUBLE) AS bench_power_w",
                "CAST(NULL AS DOUBLE) AS peak_power_w",
            )
        )
        sample_stats = ""
        join = ""

    order = "r.run_id DESC" if "run_id" in columns else "1"
    query = f"""
        {sample_stats}
        SELECT {', '.join(selections)}
        FROM runs r
        {join}
        ORDER BY {order}
    """
    runs = rows_as_dicts(connection.execute(query))
    coverage_recorded = "bench_sample_coverage" in columns
    completion_recorded = "bench_end" in columns

    for run in runs:
        run["optimization"] = run.get("optimization") or "baseline"
        run["host"] = run.get("host") or "unknown host"
        run["test"] = run.get("test") or "untitled"
        dropped = run.get("dropped_packets") or 0
        if run["test"] == IDLE_TEST:
            valid = bool(run.get("idle_samples")) and dropped == 0
            reason = "valid idle capture" if valid else "needs idle samples"
        else:
            complete = (
                run.get("bench_end") is not None
                if completion_recorded
                else run.get("bench_score") is not None
            )
            scored = run.get("bench_score") is not None
            coverage = run.get("bench_sample_coverage")
            covered = (
                coverage is not None and coverage >= 0.9
                if coverage_recorded
                else True
            )
            valid = complete and scored and covered and dropped == 0
            if not complete:
                reason = "incomplete"
            elif not scored:
                reason = "missing result"
            elif not covered:
                reason = "low sample coverage"
            elif dropped:
                reason = "dropped packets"
            else:
                reason = "valid completed run"
        run["valid"] = valid
        run["quality_reason"] = reason
    return runs


def raw_table_payload(
    connection: duckdb.DuckDBPyConnection, table: str, row_limit: int
) -> dict[str, Any]:
    columns = table_columns(connection, table)
    quoted_table = quote_identifier(table)
    row_count = connection.execute(f"SELECT count(*) FROM {quoted_table}").fetchone()[0]
    order_column = "run_id" if "run_id" in columns else columns[0] if columns else None
    query = f"SELECT * FROM {quoted_table}"
    if order_column:
        query += f" ORDER BY {quote_identifier(order_column)} DESC NULLS LAST"
    query += " LIMIT ?"
    rows = [
        [json_value(value) for value in row]
        for row in connection.execute(query, [row_limit]).fetchall()
    ]
    return {
        "name": table,
        "columns": columns,
        "rows": rows,
        "rowCount": row_count,
        "shown": len(rows),
    }


def report_payload(
    connection: duckdb.DuckDBPyConnection, row_limit: int
) -> dict[str, Any]:
    names = table_names(connection)
    name_set = set(names)
    runs = run_payload(connection, name_set)
    for run in runs:
        filename = detail_filename(run.get("run_id"))
        if filename:
            run["detail_url"] = f"runs/{filename}"
    raw_tables = [raw_table_payload(connection, table, row_limit) for table in names]
    results_by_run: dict[str, list[dict[str, Any]]] = {}
    if "run_results" in name_set:
        for run in runs:
            results_by_run[str(run["run_id"])] = run_result_payload(
                connection, name_set, int(run["run_id"])
            )
    valid_runs = [run for run in runs if run["valid"]]
    return {
        "meta": {
            "generatedAt": datetime.now().astimezone().isoformat(timespec="minutes"),
            "tableCount": len(names),
            "totalRows": sum(table["rowCount"] for table in raw_tables),
            "runCount": len(runs),
            "validRunCount": len(valid_runs),
            "hosts": sorted({run["host"] for run in runs}),
            "tests": sorted({run["test"] for run in runs}),
            "configurations": sorted({run["optimization"] for run in runs}),
        },
        "runs": runs,
        "resultsByRun": results_by_run,
        "hostConfigs": host_config_payload(runs),
        "tables": raw_tables,
    }


def safe_json_script(data: Any) -> str:
    """Encode JSON so data values cannot terminate the enclosing script element."""
    return (
        json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_run_reports(
    connection: duckdb.DuckDBPyConnection,
    tables: set[str],
    runs: list[dict[str, Any]],
    output: Path,
) -> None:
    """Write one self-contained detail page and power history chart for every run."""
    template = RUN_TEMPLATE.read_text(encoding="utf-8")
    run_dir = output.parent / "runs"
    for run in runs:
        filename = detail_filename(run.get("run_id"))
        if not filename:
            continue
        page = (
            template.replace("__RUN_DATA__", safe_json_script(run_detail_payload(connection, tables, run)))
            .replace("__RUN_ID__", escape(str(run["run_id"])))
            .replace("__REPORT_FILENAME__", escape(output.name))
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / filename).write_text(page, encoding="utf-8")


def render_report(database: Path, output: Path, row_limit: int) -> None:
    with duckdb.connect(str(database), read_only=True) as connection:
        payload = report_payload(connection, row_limit)
        tables = set(table_names(connection))
        render_run_reports(connection, tables, payload["runs"], output)

    # The full applied-config JSON stays on the per-run detail pages; the main
    # report only needs it already summarized in hostConfigs.
    embedded = {
        **payload,
        "runs": [
            {key: value for key, value in run.items() if key != "applied_config"}
            for run in payload["runs"]
        ],
    }
    report = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__REPORT_DATA__", safe_json_script(embedded))
        .replace("__GENERATED_AT__", escape(payload["meta"]["generatedAt"]))
        .replace("__DATABASE_NAME__", escape(database.name))
        .replace("__ROW_LIMIT__", f"{row_limit:,}")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--row-limit", type=int, default=DEFAULT_ROW_LIMIT)
    args = parser.parse_args()
    if args.row_limit < 1:
        parser.error("--row-limit must be at least 1")
    render_report(args.database, args.output, args.row_limit)


if __name__ == "__main__":
    main()
