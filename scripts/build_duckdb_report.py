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
HOST_METRICS = (
    ("energy_wh", "Integrated energy", "Wh"),
    ("idle_power_w", "Idle power", "W"),
    ("bench_power_w", "Load power", "W"),
    ("bench_score", "Primary benchmark result", ""),
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


def average(values: list[Any]) -> float | None:
    numbers = [
        float(value)
        for value in values
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ]
    return sum(numbers) / len(numbers) if numbers else None


def host_comparison_payload(
    connection: duckdb.DuckDBPyConnection,
    tables: set[str],
    runs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate valid runs without discarding suite/sub-test results."""
    valid_runs = [run for run in runs if run["valid"]]
    results_by_run: dict[int, list[dict[str, Any]]] = {}
    if "run_results" in tables:
        for run in valid_runs:
            results_by_run[int(run["run_id"])] = run_result_payload(
                connection, tables, int(run["run_id"])
            )

    configurations: list[dict[str, Any]] = []
    names = sorted(
        {str(run["optimization"]) for run in valid_runs},
        key=lambda name: (name.lower() != "baseline", name.lower()),
    )
    for name in names:
        config_runs = [run for run in valid_runs if run["optimization"] == name]
        tests: list[dict[str, Any]] = []
        for test_name in sorted({str(run["test"]) for run in config_runs}):
            test_runs = [run for run in config_runs if run["test"] == test_name]
            hosts: list[dict[str, Any]] = []
            for host_name in sorted({str(run["host"]) for run in test_runs}):
                host_runs = [run for run in test_runs if run["host"] == host_name]
                subtests: dict[tuple[str, str, bool | None], list[float]] = {}
                for run in host_runs:
                    for result in results_by_run.get(int(run["run_id"]), []):
                        value = result.get("value")
                        if not isinstance(value, (int, float)):
                            continue
                        key = (
                            str(result.get("title") or "Untitled result"),
                            str(result.get("scale") or ""),
                            result.get("higher_is_better"),
                        )
                        subtests.setdefault(key, []).append(float(value))
                hosts.append(
                    {
                        "host": host_name,
                        "runCount": len(host_runs),
                        "runIds": [run["run_id"] for run in host_runs],
                        "metrics": {
                            key: average([run.get(key) for run in host_runs])
                            for key, _, _ in HOST_METRICS
                        },
                        "units": sorted(
                            {
                                str(run["bench_unit"])
                                for run in host_runs
                                if present(run.get("bench_unit"))
                            }
                        ),
                        "subtests": [
                            {
                                "title": key[0],
                                "scale": key[1],
                                "higherIsBetter": key[2],
                                "mean": average(values),
                                "min": min(values),
                                "max": max(values),
                                "count": len(values),
                            }
                            for key, values in sorted(subtests.items())
                        ],
                    }
                )
            tests.append({"name": test_name, "hosts": hosts})
        configurations.append(
            {
                "name": name,
                "runCount": len(config_runs),
                "hostCount": len({run["host"] for run in config_runs}),
                "testCount": len(tests),
                "tests": tests,
            }
        )
    return configurations


def host_color(host: str) -> str:
    value = 0
    for character in host:
        value = (value * 31 + ord(character)) & 0xFFFFFFFF
    return f"hsl({value % 360} 58% 43%)"


def host_chart_svg(
    comparison: dict[str, Any], metric_key: str, metric_label: str, unit: str
) -> str:
    """Build a dependency-free SVG chart for one configuration and metric."""
    rows = []
    for test in comparison["tests"]:
        for host in test["hosts"]:
            value = host["metrics"].get(metric_key)
            if value is not None:
                rows.append((test["name"], host["host"], float(value)))
    if not rows:
        return '<div class="empty compact-empty">No values recorded for this measurement.</div>'

    width, label_width, right = 960, 280, 92
    row_height = 28
    height = max(150, 50 + len(rows) * row_height)
    maximum = max(value for _, _, value in rows) or 1
    plot_width = width - label_width - right
    bars = []
    for index, (test, host, value) in enumerate(rows):
        y = 26 + index * row_height
        bar_width = max(1, value / maximum * plot_width)
        label = f"{test} · {host}"
        shown_label = label if len(label) <= 42 else label[:40] + "…"
        suffix = f" {unit}" if unit else ""
        bars.append(
            f'<g><text x="{label_width - 10}" y="{y + 15}" text-anchor="end">'
            f'{escape(shown_label)}</text><rect x="{label_width}" y="{y + 2}" '
            f'width="{bar_width:.2f}" height="18" rx="4" fill="{host_color(host)}">'
            f'<title>{escape(label)}: {value:.4g}{escape(suffix)}</title></rect>'
            f'<text x="{min(width - right + 7, label_width + bar_width + 7):.2f}" '
            f'y="{y + 15}">{value:.4g}{escape(suffix)}</text></g>'
        )
    return (
        f'<svg class="built-chart" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{escape(metric_label)} by host and test">'
        '<style>text{font:11px system-ui;fill:var(--ink)}</style>'
        f'<line x1="{label_width}" x2="{label_width}" y1="10" y2="{height - 20}" '
        'stroke="var(--line)"/>'
        + "".join(bars)
        + f'<text x="{label_width}" y="{height - 3}" fill="var(--muted)">0</text>'
        + f'<text x="{width - right}" y="{height - 3}" text-anchor="end" '
        + f'fill="var(--muted)">{maximum:.4g}{escape(" " + unit if unit else "")}</text></svg>'
    )


def comparison_number(value: Any, unit: str = "") -> str:
    if value is None:
        return "—"
    suffix = f" {unit}" if unit else ""
    return f"{float(value):,.3g}{suffix}"


def host_comparison_html(comparisons: list[dict[str, Any]]) -> str:
    if not comparisons:
        return '<div class="empty panel">No valid host comparisons are available.</div>'
    cards = []
    for comparison in comparisons:
        charts = "".join(
            f'<div class="built-host-chart" data-host-metric="{escape(key)}" '
            f'{"" if key == "energy_wh" else "hidden"}>{host_chart_svg(comparison, key, label, unit)}</div>'
            for key, label, unit in HOST_METRICS
        )
        test_sections = []
        for test in comparison["tests"]:
            hosts = test["hosts"]
            reference = hosts[0] if hosts else None
            reference_energy = (
                reference["metrics"].get("energy_wh") if reference else None
            )
            result_rows = []
            for host in hosts:
                energy = host["metrics"].get("energy_wh")
                delta = (
                    (energy / reference_energy - 1) * 100
                    if energy is not None and reference_energy
                    else None
                )
                run_links = ", ".join(
                    f'<a href="runs/{int(run_id)}.html">#{int(run_id)}</a>'
                    for run_id in host["runIds"]
                )
                unit = host["units"][0] if len(host["units"]) == 1 else ""
                result_rows.append(
                    f'<tr><th scope="row">{escape(host["host"])}</th>'
                    f'<td class="num">{comparison_number(host["metrics"].get("bench_score"), unit)}</td>'
                    f'<td class="num">{comparison_number(energy, "Wh")}</td>'
                    f'<td class="num {"positive" if delta is not None and delta < 0 else "negative" if delta is not None and delta > 0 else "neutral"}">'
                    f'{"reference" if host is reference else comparison_number(delta, "%")}</td>'
                    f'<td class="num">{comparison_number(host["metrics"].get("idle_power_w"), "W")}</td>'
                    f'<td class="num">{comparison_number(host["metrics"].get("bench_power_w"), "W")}</td>'
                    f'<td>{run_links}</td></tr>'
                )
            subtest_keys = sorted(
                {
                    (item["title"], item["scale"], item["higherIsBetter"])
                    for host in hosts
                    for item in host["subtests"]
                }
            )
            subtest_rows = []
            for title, scale, direction in subtest_keys:
                cells = []
                for host in hosts:
                    result = next(
                        (
                            item
                            for item in host["subtests"]
                            if (
                                item["title"],
                                item["scale"],
                                item["higherIsBetter"],
                            )
                            == (title, scale, direction)
                        ),
                        None,
                    )
                    if result:
                        spread = (
                            f'<span class="secondary">{result["min"]:,.3g}–{result["max"]:,.3g}; '
                            f'n={result["count"]}</span>'
                        )
                        cells.append(
                            f'<td class="num">{result["mean"]:,.3g}{spread}</td>'
                        )
                    else:
                        cells.append('<td class="num neutral">—</td>')
                direction_text = (
                    "higher is better"
                    if direction is True
                    else "lower is better"
                    if direction is False
                    else "direction not recorded"
                )
                subtest_rows.append(
                    f'<tr><th scope="row">{escape(title)}<span class="secondary">'
                    f'{escape(scale or "unit not recorded")} · {direction_text}</span></th>'
                    + "".join(cells)
                    + "</tr>"
                )
            subtests = (
                '<div class="table-wrap subtests"><table><thead><tr><th>Sub-test</th>'
                + "".join(f'<th class="num">{escape(host["host"])}</th>' for host in hosts)
                + "</tr></thead><tbody>"
                + "".join(subtest_rows)
                + "</tbody></table></div>"
                if subtest_rows
                else '<p class="no-subtests">No suite sub-tests were recorded for these runs.</p>'
            )
            test_sections.append(
                f'<details class="test-result" data-host-test="{escape(test["name"])}">'
                f'<summary>{escape(test["name"])} <span class="caption">{len(hosts)} hosts</span></summary>'
                '<div class="table-wrap"><table class="host-results"><thead><tr>'
                '<th>Host</th><th class="num">Primary result</th><th class="num">Energy</th>'
                f'<th class="num">Energy vs {escape(reference["host"] if reference else "reference")}</th>'
                '<th class="num">Idle power</th><th class="num">Load power</th><th>Runs</th>'
                "</tr></thead><tbody>"
                + "".join(result_rows)
                + "</tbody></table></div>"
                + subtests
                + "</details>"
            )
        cards.append(
            f'<details class="host-comparison" data-host-configuration="{escape(comparison["name"])}">'
            f'<summary><span>{escape(comparison["name"])}</span><span class="caption">'
            f'{comparison["hostCount"]} hosts · {comparison["testCount"]} tests · '
            f'{comparison["runCount"]} valid runs</span></summary>'
            f'<div class="host-comparison-body"><div class="host-svg-frame">{charts}</div>'
            '<div class="test-results"><h3>Test results and recorded sub-tests</h3>'
            + "".join(test_sections)
            + "</div></div></details>"
        )
    return "".join(cards)


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
        comparisons = host_comparison_payload(connection, tables, payload["runs"])
        comparison_html = host_comparison_html(comparisons)
        render_run_reports(connection, tables, payload["runs"], output)

    report = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("__REPORT_DATA__", safe_json_script(payload))
        .replace("__HOST_COMPARISONS__", comparison_html)
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
