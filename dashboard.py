#!/usr/bin/env python3
"""Interactive, read-only browser for the power-benchmark DuckDB database.

Run from the repository root:

    python3 dashboard.py

Then open http://127.0.0.1:8080.  The server intentionally has no write
endpoints; it is safe to leave running while benchmark data is being captured.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import duckdb

from run_suite import EXPERIMENTS, IDLE_TEST, PERF_FLOOR_TEST


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "benchmarks" / "power_meter.duckdb"
MAX_RUNS = 500
MAX_PLOT_POINTS = 450
PLANNED_TARGETS = {label: target for label, _overrides, target in EXPERIMENTS}


def json_default(value: Any) -> Any:
    """Make DuckDB values safe to pass through the JSON API."""
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ")
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Cannot encode {type(value).__name__}")


def rows_as_dicts(cursor: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class DatabaseReader:
    """Short-lived, read-only queries that tolerate an active benchmark writer."""

    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> duckdb.DuckDBPyConnection:
        return duckdb.connect(str(self.path), read_only=True)

    def table_names(self, conn: duckdb.DuckDBPyConnection) -> set[str]:
        return {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'"
            ).fetchall()
        }

    def run_columns(self, conn: duckdb.DuckDBPyConnection) -> set[str]:
        return {
            row[1]
            for row in conn.execute("PRAGMA table_info('runs')").fetchall()
        }

    def overview(self) -> dict[str, Any]:
        with self.connect() as conn:
            tables = self.table_names(conn)
            if "runs" not in tables:
                return {"database": str(self.path), "available": False, "reason": "No runs table found."}
            columns = self.run_columns(conn)
            completed = "bench_end IS NOT NULL" if "bench_end" in columns else "FALSE"
            metrics = conn.execute(
                f"""
                SELECT
                    count(*) AS run_count,
                    count(*) FILTER (WHERE {completed}) AS completed_count,
                    count(DISTINCT optimization) AS optimization_count,
                    count(DISTINCT test) AS test_count,
                    max(started_at) AS latest_started_at
                FROM runs
                """
            )
            summary = rows_as_dicts(metrics)[0]
            summary["reading_count"] = (
                conn.execute("SELECT count(*) FROM readings").fetchone()[0]
                if "readings" in tables
                else 0
            )
            summary.update({"database": str(self.path), "available": True})
            return summary

    def filters(self) -> dict[str, list[str]]:
        with self.connect() as conn:
            if "runs" not in self.table_names(conn):
                return {"optimizations": [], "tests": [], "hosts": []}
            values: dict[str, list[str]] = {}
            for column, key in (("optimization", "optimizations"), ("test", "tests"), ("host", "hosts")):
                values[key] = [
                    row[0]
                    for row in conn.execute(
                        f"SELECT DISTINCT {quote_identifier(column)} FROM runs "
                        f"WHERE {quote_identifier(column)} IS NOT NULL "
                        f"ORDER BY {quote_identifier(column)}"
                    ).fetchall()
                ]
            return values

    def runs(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if "runs" not in self.table_names(conn):
                return []
            columns = self.run_columns(conn)
            where: list[str] = []
            params: list[Any] = []
            for query_name, column in (("optimization", "optimization"), ("test", "test"), ("host", "host")):
                value = filters.get(query_name, "")
                if value:
                    where.append(f"{quote_identifier(column)} = ?")
                    params.append(value)
            status = filters.get("status", "all")
            if status == "complete" and "bench_end" in columns:
                where.append("bench_end IS NOT NULL")
            elif status == "incomplete" and "bench_end" in columns:
                where.append("bench_end IS NULL")
            search = filters.get("q", "").strip()
            if search:
                searchable = [name for name in ("optimization", "test", "host", "result_name", "config_hash") if name in columns]
                where.append("(" + " OR ".join(f"coalesce(cast({quote_identifier(name)} AS VARCHAR), '') ILIKE ?" for name in searchable) + ")")
                params.extend([f"%{search}%"] * len(searchable))
            selected = [
                "run_id", "started_at", "test", "optimization", "repeat_idx", "host",
                "bench_score", "bench_unit", "higher_is_better", "energy_wh_integrated",
                "dropped_packets", "checksum_failures", "bench_sample_coverage", "ambient_c",
                "bench_start_temp_c", "governor", "turbo", "bench_end",
            ]
            select_columns = [name for name in selected if name in columns]
            sql = f"SELECT {', '.join(quote_identifier(name) for name in select_columns)} FROM runs"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY started_at DESC, run_id DESC LIMIT ?"
            params.append(MAX_RUNS)
            return rows_as_dicts(conn.execute(sql, params))

    def aggregates(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        """Give the comparison chart one row per test/optimization pair."""
        with self.connect() as conn:
            if "runs" not in self.table_names(conn):
                return []
            columns = self.run_columns(conn)
            conditions = ["optimization IS NOT NULL"]
            params: list[Any] = []
            for query_name, column in (("test", "test"), ("host", "host")):
                if filters.get(query_name):
                    conditions.append(f"{quote_identifier(column)} = ?")
                    params.append(filters[query_name])
            if filters.get("optimization"):
                conditions.append("optimization = ?")
                params.append(filters["optimization"])
            if "bench_end" in columns:
                conditions.append("bench_end IS NOT NULL")
            coverage = "avg(bench_sample_coverage)" if "bench_sample_coverage" in columns else "NULL"
            sql = f"""
                SELECT optimization, test, count(*) AS run_count,
                       avg(energy_wh_integrated) AS average_energy_wh,
                       avg(bench_score) AS average_bench_score,
                       avg(ambient_c) AS average_ambient_c,
                       {coverage} AS average_coverage
                FROM runs
                WHERE {' AND '.join(conditions)}
                GROUP BY optimization, test
                ORDER BY average_energy_wh NULLS LAST, optimization
            """
            return rows_as_dicts(conn.execute(sql, params))

    def coverage(self) -> list[dict[str, Any]]:
        """Return the planned host/test matrix together with recorded results."""
        with self.connect() as conn:
            tables = self.table_names(conn)
            if "runs" not in tables:
                return []
            columns = self.run_columns(conn)
            dropped = "coalesce(r.dropped_packets, 0)" if "dropped_packets" in columns else "0"
            sample_coverage = "coalesce(r.bench_sample_coverage, 0)" if "bench_sample_coverage" in columns else "0"
            bench_end = "r.bench_end IS NOT NULL" if "bench_end" in columns else "FALSE"
            bench_score = "r.bench_score IS NOT NULL" if "bench_score" in columns else "FALSE"
            bench_unit = "r.bench_unit" if "bench_unit" in columns else "NULL"
            readings_cte = (
                """
                run_readings AS (
                    SELECT run_id,
                           count(*) FILTER (WHERE phase = 'idle') AS idle_samples,
                           avg(power_w) FILTER (WHERE phase = 'idle') AS idle_power_w
                    FROM readings
                    GROUP BY run_id
                )
                """
                if "readings" in tables
                else """
                run_readings AS (
                    SELECT CAST(NULL AS INTEGER) AS run_id,
                           CAST(0 AS BIGINT) AS idle_samples,
                           CAST(NULL AS DOUBLE) AS idle_power_w
                    WHERE FALSE
                )
                """
            )
            valid = f"""
                CASE
                    WHEN coalesce(r.test, '') = ?
                        THEN coalesce(rr.idle_samples, 0) > 0 AND {dropped} = 0
                    ELSE {bench_end} AND {bench_score} AND {sample_coverage} >= 0.9 AND {dropped} = 0
                END
            """
            rows = rows_as_dicts(conn.execute(
                f"""
                WITH {readings_cte}
                SELECT coalesce(r.host, 'unknown host') AS host,
                       coalesce(r.optimization, 'baseline') AS optimization,
                       coalesce(r.test, 'untitled') AS test,
                       count(*) AS run_count,
                       count(*) FILTER (WHERE {valid}) AS valid_count,
                       avg(r.bench_score) AS average_bench_score,
                       max({bench_unit}) AS bench_unit,
                       avg(r.energy_wh_integrated) AS average_energy_wh,
                       avg(rr.idle_power_w) AS average_idle_power_w,
                       max(r.run_id) AS latest_run_id,
                       max(r.started_at) AS latest_started_at
                FROM runs r
                LEFT JOIN run_readings rr ON rr.run_id = r.run_id
                GROUP BY 1, 2, 3
                """,
                [IDLE_TEST],
            ))

        recorded = {(row["host"], row["optimization"], row["test"]): row for row in rows}
        hosts = sorted({row["host"] for row in rows})
        observed_tests = {row["test"] for row in rows}
        tests = [IDLE_TEST, PERF_FLOOR_TEST] + sorted(observed_tests - {IDLE_TEST, PERF_FLOOR_TEST})
        planned_optimizations = list(PLANNED_TARGETS)
        optimizations = planned_optimizations + sorted(
            {row["optimization"] for row in rows} - set(planned_optimizations)
        )
        matrix: list[dict[str, Any]] = []
        for host in hosts:
            for optimization in optimizations:
                target = PLANNED_TARGETS.get(optimization)
                for test in tests:
                    item = dict(recorded.get((host, optimization, test), {}))
                    item.update({"host": host, "optimization": optimization, "test": test})
                    planned = self.planned_test(optimization, test, target)
                    item["planned"] = planned
                    if not item.get("run_count"):
                        item.update({
                            "run_count": 0, "valid_count": 0,
                            "average_bench_score": None, "bench_unit": None,
                            "average_energy_wh": None, "average_idle_power_w": None,
                            "latest_run_id": None, "latest_started_at": None,
                            "status": "missing" if planned else "skipped",
                        })
                    elif item["valid_count"] == item["run_count"]:
                        item["status"] = "complete"
                    elif item["valid_count"]:
                        item["status"] = "attention"
                    else:
                        item["status"] = "incomplete"
                    matrix.append(item)
        return matrix

    def host_comparisons(self) -> list[dict[str, Any]]:
        """Return valid-run measurement ranges grouped by host, test, and configuration."""
        with self.connect() as conn:
            tables = self.table_names(conn)
            if "runs" not in tables:
                return []
            columns = self.run_columns(conn)
            dropped = "coalesce(r.dropped_packets, 0)" if "dropped_packets" in columns else "0"
            coverage = "coalesce(r.bench_sample_coverage, 0)" if "bench_sample_coverage" in columns else "0"
            complete = "r.bench_end IS NOT NULL" if "bench_end" in columns else "FALSE"
            scored = "r.bench_score IS NOT NULL" if "bench_score" in columns else "FALSE"
            energy = "r.energy_wh_integrated" if "energy_wh_integrated" in columns else "NULL"
            score = "r.bench_score" if "bench_score" in columns else "NULL"
            readings = """
                run_readings AS (
                    SELECT run_id, count(*) FILTER (WHERE phase = 'idle') AS idle_samples,
                           avg(power_w) FILTER (WHERE phase = 'idle') AS idle_power_w,
                           avg(power_w) FILTER (WHERE phase = 'bench') AS bench_power_w
                    FROM readings GROUP BY run_id
                )
            """ if "readings" in tables else """
                run_readings AS (
                    SELECT CAST(NULL AS INTEGER) AS run_id, CAST(0 AS BIGINT) AS idle_samples,
                           CAST(NULL AS DOUBLE) AS idle_power_w, CAST(NULL AS DOUBLE) AS bench_power_w WHERE FALSE
                )
            """
            valid = f"""CASE WHEN coalesce(r.test, '') = ? THEN coalesce(rr.idle_samples, 0) > 0 AND {dropped} = 0 ELSE {complete} AND {scored} AND {coverage} >= 0.9 AND {dropped} = 0 END"""
            return rows_as_dicts(conn.execute(
                f"""
                WITH {readings}
                SELECT coalesce(r.host, 'unknown host') AS host,
                       coalesce(r.optimization, 'baseline') AS optimization,
                       coalesce(r.test, 'untitled') AS test,
                       count(*) FILTER (WHERE {valid}) AS valid_count,
                       avg({energy}) FILTER (WHERE {valid}) AS average_energy_wh,
                       min({energy}) FILTER (WHERE {valid}) AS minimum_energy_wh,
                       max({energy}) FILTER (WHERE {valid}) AS maximum_energy_wh,
                       avg(rr.idle_power_w) FILTER (WHERE {valid}) AS average_idle_power_w,
                       min(rr.idle_power_w) FILTER (WHERE {valid}) AS minimum_idle_power_w,
                       max(rr.idle_power_w) FILTER (WHERE {valid}) AS maximum_idle_power_w,
                       avg(rr.bench_power_w) FILTER (WHERE {valid}) AS average_bench_power_w,
                       min(rr.bench_power_w) FILTER (WHERE {valid}) AS minimum_bench_power_w,
                       max(rr.bench_power_w) FILTER (WHERE {valid}) AS maximum_bench_power_w,
                       avg({score}) FILTER (WHERE {valid}) AS average_bench_score,
                       min({score}) FILTER (WHERE {valid}) AS minimum_bench_score,
                       max({score}) FILTER (WHERE {valid}) AS maximum_bench_score
                FROM runs r LEFT JOIN run_readings rr ON rr.run_id = r.run_id
                GROUP BY 1, 2, 3 ORDER BY test, optimization, host
                """,
                [IDLE_TEST] * 13,
            ))
    @staticmethod
    def planned_test(optimization: str, test: str, target: str | None) -> bool:
        """Whether the suite catalog intentionally schedules a test for a variant."""
        if target is None:
            return True  # Custom DB variants have no catalog guidance.
        if target == "idle":
            return test in {IDLE_TEST, PERF_FLOOR_TEST}
        if optimization == "baseline":
            return True
        return test != IDLE_TEST

    def run_detail(self, run_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            tables = self.table_names(conn)
            if "runs" not in tables:
                return None
            result = rows_as_dicts(conn.execute("SELECT * FROM runs WHERE run_id = ?", [run_id]))
            if not result:
                return None
            run = result[0]
            run["applied_config_parsed"] = parse_json(run.get("applied_config"))
            run["phase_stats"] = self.phase_stats(conn, run_id, tables)
            run["results"] = self.results(conn, run_id, tables)
            run["samples"] = self.samples(conn, run_id, tables)
            return run

    def phase_stats(self, conn: duckdb.DuckDBPyConnection, run_id: int, tables: set[str]) -> list[dict[str, Any]]:
        if "readings" not in tables:
            return []
        return rows_as_dicts(conn.execute(
            """
            SELECT phase, count(*) AS samples, min(power_w) AS min_power_w,
                   avg(power_w) AS average_power_w, max(power_w) AS max_power_w,
                   min(temperature_c) AS min_temperature_c,
                   max(temperature_c) AS max_temperature_c
            FROM readings
            WHERE run_id = ?
            GROUP BY phase
            ORDER BY CASE phase WHEN 'settle' THEN 1 WHEN 'idle' THEN 2
                                WHEN 'bench' THEN 3 WHEN 'cooldown' THEN 4 ELSE 5 END
            """,
            [run_id],
        ))

    def results(self, conn: duckdb.DuckDBPyConnection, run_id: int, tables: set[str]) -> list[dict[str, Any]]:
        if "run_results" not in tables:
            return []
        return rows_as_dicts(conn.execute(
            "SELECT title, scale, higher_is_better, value FROM run_results WHERE run_id = ? ORDER BY title",
            [run_id],
        ))

    def samples(self, conn: duckdb.DuckDBPyConnection, run_id: int, tables: set[str]) -> list[dict[str, Any]]:
        if "readings" not in tables:
            return []
        count = conn.execute("SELECT count(*) FROM readings WHERE run_id = ?", [run_id]).fetchone()[0]
        if not count:
            return []
        stride = max(1, math.ceil(count / MAX_PLOT_POINTS))
        return rows_as_dicts(conn.execute(
            """
            WITH numbered AS (
                SELECT timestamp, phase, power_w, voltage_v, current_a, temperature_c,
                       row_number() OVER (ORDER BY timestamp) AS sample_number
                FROM readings
                WHERE run_id = ?
            )
            SELECT timestamp, phase, power_w, voltage_v, current_a, temperature_c
            FROM numbered
            WHERE sample_number = 1 OR sample_number % ? = 0 OR sample_number = ?
            ORDER BY timestamp
            """,
            [run_id, stride, count],
        ))


def parse_json(value: Any) -> dict[str, Any] | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "PowerBenchDashboard/1.0"

    @property
    def reader(self) -> DatabaseReader:
        return self.server.reader  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        # Retain useful access logs without the default noisy reverse-DNS attempt.
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:  # noqa: N802 (HTTP method name is prescribed)
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self.send_bytes(HTTPStatus.OK, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/overview":
            self.send_json(self.reader.overview())
            return
        if parsed.path == "/api/filters":
            self.send_json(self.reader.filters())
            return
        if parsed.path == "/api/runs":
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
            self.send_json(self.reader.runs(query))
            return
        if parsed.path == "/api/aggregates":
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items() if values}
            self.send_json(self.reader.aggregates(query))
            return
        if parsed.path == "/api/host-comparisons":
            self.send_json(self.reader.host_comparisons())
            return
        if parsed.path == "/api/coverage":
            self.send_json(self.reader.coverage())
            return
        if parsed.path.startswith("/api/runs/"):
            run_id_text = parsed.path.removeprefix("/api/runs/")
            try:
                run_id = int(run_id_text)
            except ValueError:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Run id must be an integer.")
                return
            detail = self.reader.run_detail(run_id)
            if detail is None:
                self.send_error_json(HTTPStatus.NOT_FOUND, f"Run {run_id} was not found.")
                return
            self.send_json(detail)
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def send_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_bytes(status, json.dumps(data, default=json_default, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8")

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json({"error": message}, status)

    def send_bytes(self, status: HTTPStatus, data: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


PAGE = r'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Power Bench · Run Explorer</title>
  <style>
    :root { --ink:#202a2b; --muted:#607070; --line:#d8e0dc; --paper:#f7f8f3; --panel:#fffefa; --teal:#087e78; --teal-dark:#05635f; --lime:#b9d83d; --amber:#e99c28; --rose:#c94e56; --navy:#173f53; }
    * { box-sizing:border-box; }
    body { margin:0; color:var(--ink); background:linear-gradient(145deg,#e8f0e7 0,#f7f8f3 37%,#f2f5ee 100%); font:14px/1.45 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    button,select,input { font:inherit; }
    button { cursor:pointer; }
    .shell { max-width:1560px; margin:0 auto; padding:28px 28px 44px; }
    header { display:flex; justify-content:space-between; align-items:flex-start; gap:24px; margin-bottom:24px; }
    .eyebrow { margin:0 0 4px; color:var(--teal-dark); font-weight:750; font-size:12px; letter-spacing:.12em; text-transform:uppercase; }
    h1 { margin:0; font-family:Georgia,"Times New Roman",serif; font-size:clamp(29px,4vw,45px); line-height:1.02; letter-spacing:-.035em; }
    .subhead { max-width:660px; margin:9px 0 0; color:var(--muted); font-size:15px; }
    .refresh { border:1px solid var(--teal); border-radius:8px; color:white; background:var(--teal); padding:9px 13px; font-weight:700; box-shadow:0 2px 0 var(--teal-dark); }
    .refresh:hover { background:var(--teal-dark); }
    .summary { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; margin-bottom:18px; }
    .stat,.panel { border:1px solid var(--line); border-radius:10px; background:rgba(255,254,250,.88); box-shadow:0 5px 16px rgba(38,58,51,.055); }
    .stat { min-height:96px; padding:15px 16px; }
    .stat .value { display:block; font:700 28px/1 Georgia,serif; letter-spacing:-.04em; }
    .stat .label { display:block; margin-top:7px; color:var(--muted); font-size:12px; }
    .filters { display:grid; grid-template-columns:1.15fr 1.35fr 1fr 1fr 1.5fr auto; gap:10px; align-items:end; padding:14px; margin-bottom:18px; }
    .field { min-width:0; }
    label { display:block; margin:0 0 4px; color:var(--muted); font-size:11px; font-weight:750; letter-spacing:.08em; text-transform:uppercase; }
    select,input { width:100%; border:1px solid #bdcbc4; border-radius:7px; color:var(--ink); background:#fffefa; padding:8px 9px; outline:none; }
    select:focus,input:focus { border-color:var(--teal); box-shadow:0 0 0 3px rgba(8,126,120,.13); }
    .clear { border:0; border-bottom:1px solid var(--teal); color:var(--teal-dark); background:transparent; padding:8px 2px; font-weight:700; }
    .main { display:grid; grid-template-columns:minmax(560px,1.15fr) minmax(420px,.85fr); gap:18px; align-items:start; }
    .panel-head { display:flex; justify-content:space-between; align-items:center; gap:12px; padding:16px 16px 10px; }
    .panel-head h2 { margin:0; font-family:Georgia,serif; font-size:20px; letter-spacing:-.02em; }
    .caption { color:var(--muted); font-size:12px; }
    .table-wrap { overflow:auto; max-height:715px; border-top:1px solid var(--line); }
    table { width:100%; border-collapse:collapse; white-space:nowrap; font-variant-numeric:tabular-nums; }
    th { position:sticky; top:0; z-index:1; border-bottom:1px solid var(--line); color:#50605c; background:#f0f3ed; padding:9px 10px; text-align:left; font-size:10px; letter-spacing:.07em; text-transform:uppercase; }
    td { border-bottom:1px solid #e8ece6; padding:10px; vertical-align:middle; }
    tr.run { cursor:pointer; transition:background .12s; }
    tr.run:hover { background:#eff7ed; }
    tr.run.selected { background:#dff1e9; box-shadow:inset 3px 0 var(--teal); }
    .run-name { max-width:208px; overflow:hidden; text-overflow:ellipsis; font-weight:700; }
    .secondary { display:block; max-width:180px; overflow:hidden; color:var(--muted); font-size:11px; text-overflow:ellipsis; }
    .num { text-align:right; }
    .chip { display:inline-flex; border-radius:99px; padding:3px 7px; font-size:11px; font-weight:750; }
    .good { color:#13693d; background:#dff3e4; }.warn { color:#89500b; background:#fff0c9; }.bad { color:#a12b36; background:#fbe0e2; }.neutral { color:#45625e; background:#e5ece8; }
    .detail { min-height:650px; overflow:hidden; }
    .empty { display:grid; min-height:600px; place-items:center; padding:36px; text-align:center; color:var(--muted); }
    .empty strong { display:block; margin-bottom:6px; color:var(--ink); font:24px Georgia,serif; }
    .detail-content { display:none; }
    .detail-content.visible { display:block; }
    .run-title { padding:17px 18px 14px; border-bottom:1px solid var(--line); background:linear-gradient(105deg,#eff7ed,#fffefa); }
    .run-title h2 { margin:2px 0 3px; font:700 24px/1.12 Georgia,serif; letter-spacing:-.025em; }
    .run-title .meta { color:var(--muted); font-size:12px; }
    .metrics { display:grid; grid-template-columns:repeat(3,1fr); border-bottom:1px solid var(--line); }
    .metric { min-height:80px; padding:12px 14px; border-right:1px solid var(--line); }.metric:last-child{border-right:0}
    .metric small { display:block; color:var(--muted); font-size:10px; font-weight:750; letter-spacing:.07em; text-transform:uppercase; }.metric strong { display:block; margin-top:5px; font:700 20px/1 Georgia,serif; letter-spacing:-.025em; }
    .detail-section { padding:15px 16px; border-bottom:1px solid var(--line); }.detail-section:last-child { border-bottom:0; }
    h3 { margin:0 0 9px; font-size:11px; color:#50605c; letter-spacing:.09em; text-transform:uppercase; }
    .phase-grid { display:grid; grid-template-columns:repeat(2,1fr); gap:8px; }
    .phase { border:1px solid var(--line); border-left:4px solid var(--teal); border-radius:7px; padding:9px; }.phase.bench{border-left-color:var(--amber)}.phase.cooldown{border-left-color:var(--navy)}
    .phase .phase-name { color:var(--muted); font-size:11px; text-transform:capitalize; }.phase strong { display:block; margin:1px 0; font-size:16px; }.phase small { color:var(--muted); }
    #plot { display:block; width:100%; height:196px; border:1px solid var(--line); border-radius:7px; background:#fcfdf9; }
    .legend { display:flex; flex-wrap:wrap; gap:11px; margin-top:7px; color:var(--muted); font-size:11px; }.key { display:inline-flex; align-items:center; gap:5px; }.dot { width:8px;height:8px;border-radius:50%;background:var(--teal) }.dot.idle{background:var(--lime)}.dot.bench{background:var(--amber)}.dot.cooldown{background:var(--navy)}
    .properties { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0 16px; }.property { display:flex; gap:9px; justify-content:space-between; padding:7px 0; border-bottom:1px solid #e7ece6; }.property:nth-last-child(-n+2){border-bottom:0}.property dt{color:var(--muted);overflow-wrap:anywhere}.property dd{margin:0;text-align:right;font-weight:650;overflow-wrap:anywhere}.property dd.long{max-width:62%}
    .results-table { font-size:12px; }.results-table th{position:static}.results-table td{padding:7px 8px;white-space:normal}
    .comparison { margin-top:18px; }.compare-list{padding:0 16px 16px}.compare-row{display:grid;grid-template-columns:minmax(155px,1fr) minmax(120px,2.2fr) 74px;gap:10px;align-items:center;margin:9px 0}.compare-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;font-weight:650}.bar-track{height:17px;border-radius:4px;background:#e5ebe5;overflow:hidden}.bar{height:100%;min-width:2px;border-radius:4px;background:linear-gradient(90deg,var(--teal),#47a78c)}.bar-value{text-align:right;font-size:12px;font-variant-numeric:tabular-nums}.error{margin:12px 0;padding:10px 12px;border:1px solid #e6b4b8;border-radius:7px;color:#8a2630;background:#fff0f1}.loading{color:var(--muted)}
    .header-actions { display:flex; gap:12px; align-items:center; }.view-switch { display:flex; gap:3px; padding:3px; border:1px solid var(--line); border-radius:9px; background:#edf1eb; }.view-switch button { border:0; border-radius:6px; color:var(--muted); background:transparent; padding:6px 9px; font-size:12px; font-weight:700; }.view-switch button[aria-selected="true"] { color:white; background:var(--teal); }.page-view[hidden] { display:none; }.coverage-toolbar { display:grid; grid-template-columns:minmax(190px,1fr) minmax(240px,1.6fr) minmax(180px,1fr); gap:10px; padding:0 16px 16px; }.coverage-summary { display:flex; flex-wrap:wrap; gap:7px; padding:0 16px 13px; }.coverage-summary .chip { border:1px solid transparent; }.coverage-wrap { overflow:auto; max-height:calc(100vh - 300px); min-height:540px; border-top:1px solid var(--line); }.coverage-table td { white-space:normal; }.coverage-table .test-name { min-width:240px; max-width:330px; overflow-wrap:anywhere; }.coverage-table .optimization-name { min-width:205px; font-weight:700; }.coverage-table .result { min-width:150px; }.coverage-table .latest { min-width:130px; }.status-complete { color:#13693d; background:#dff3e4; }.status-attention { color:#89500b; background:#fff0c9; }.status-incomplete { color:#a12b36; background:#fbe0e2; }.status-missing { color:#89500b; background:#fff0c9; }.status-skipped { color:#45625e; background:#e5ece8; }.open-run { border:0; border-bottom:1px solid var(--teal); color:var(--teal-dark); background:transparent; padding:0; font-size:11px; font-weight:700; }
    .host-compare-toolbar{display:grid;grid-template-columns:repeat(4,minmax(150px,1fr));gap:10px;padding:0 16px 16px}.host-charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;padding:0 16px 16px}.host-chart{height:390px;padding:8px}.host-chart canvas{width:100%!important;height:100%!important}@media(max-width:900px){.host-compare-toolbar,.host-charts{grid-template-columns:repeat(2,minmax(0,1fr))}}
    @media (max-width:1100px){.summary{grid-template-columns:repeat(3,1fr)}.main{grid-template-columns:1fr}.table-wrap{max-height:510px}.detail{min-height:0}.empty{min-height:220px}}
    @media (max-width:720px){.shell{padding:19px 14px 30px}header{display:block}.header-actions{margin-top:13px;justify-content:space-between}.refresh{margin-top:0}.summary{grid-template-columns:repeat(2,1fr)}.filters,.coverage-toolbar{grid-template-columns:1fr 1fr}.filters .field:last-of-type{grid-column:span 2}.coverage-toolbar .field:last-child{grid-column:span 2}.clear{justify-self:start}.metrics{grid-template-columns:1fr}.metric{border-right:0;border-bottom:1px solid var(--line)}.metric:last-child{border-bottom:0}.properties{grid-template-columns:1fr}.property:nth-last-child(2){border-bottom:1px solid #e7ece6}.compare-row{grid-template-columns:115px 1fr 62px}.phase-grid{grid-template-columns:1fr}.run-title h2{font-size:21px}}
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div><p class="eyebrow">DuckDB run explorer</p><h1>Power bench, in context.</h1><p class="subhead">Compare every recorded test run, inspect the configuration that produced it, and trace its power readings through idle and load.</p></div>
      <div class="header-actions"><div class="view-switch" role="tablist" aria-label="Dashboard view"><button id="explorerTab" type="button" role="tab" aria-controls="explorerView" aria-selected="true">Run explorer</button><button id="hostCompareTab" type="button" role="tab" aria-controls="hostCompareView" aria-selected="false">Host comparison</button><button id="coverageTab" type="button" role="tab" aria-controls="coverageView" aria-selected="false">Test coverage</button></div><button class="refresh" id="refresh" type="button">↻ Refresh data</button></div>
    </header>
    <section class="summary" aria-label="Database summary">
      <div class="stat"><span class="value" id="runCount">—</span><span class="label">recorded runs</span></div>
      <div class="stat"><span class="value" id="completeCount">—</span><span class="label">completed runs</span></div>
      <div class="stat"><span class="value" id="optimizationCount">—</span><span class="label">optimizations</span></div>
      <div class="stat"><span class="value" id="readingCount">—</span><span class="label">power samples</span></div>
      <div class="stat"><span class="value" id="latestRun">—</span><span class="label">latest run</span></div>
    </section>
    <section class="page-view" id="explorerView">
    <section class="filters panel" aria-label="Filter runs">
      <div class="field"><label for="optimization">Optimization</label><select id="optimization"><option value="">All optimizations</option></select></div>
      <div class="field"><label for="test">Test</label><select id="test"><option value="">All tests</option></select></div>
      <div class="field"><label for="host">Host</label><select id="host"><option value="">All hosts</option></select></div>
      <div class="field"><label for="status">Run status</label><select id="status"><option value="all">All runs</option><option value="complete">Completed</option><option value="incomplete">In progress / incomplete</option></select></div>
      <div class="field"><label for="search">Find text</label><input id="search" type="search" placeholder="Name, hash, host…"></div>
      <button class="clear" id="clearFilters" type="button">Clear filters</button>
    </section>
    <section class="main">
      <section class="panel" aria-label="Recorded runs">
        <div class="panel-head"><h2>Recorded runs</h2><span class="caption" id="resultCount">Loading…</span></div>
        <div class="table-wrap"><table><thead><tr><th>Run</th><th>Optimization</th><th class="num">Result</th><th class="num">Energy</th><th>Quality</th></tr></thead><tbody id="runs"><tr><td colspan="5" class="loading">Loading runs…</td></tr></tbody></table></div>
      </section>
      <aside class="panel detail" aria-live="polite">
        <div class="empty" id="emptyDetail"><div><strong>Select a run</strong>Click a row to see its result, the variables captured at runtime, and the sampled power trace.</div></div>
        <div class="detail-content" id="detail"></div>
      </aside>
    </section>
    <section class="panel comparison"><div class="panel-head"><div><h2>Average completed-run energy</h2><span class="caption">Filtered by test and host; each bar is an optimization/test group.</span></div></div><div class="compare-list" id="comparison"><span class="loading">Loading comparison…</span></div></section>
    </section>
    <section class="page-view" id="coverageView" hidden>
      <section class="panel" aria-labelledby="coverageTitle">
        <div class="panel-head"><div><h2 id="coverageTitle">Test coverage</h2><span class="caption">Every planned host, optimization, and test combination. Results are averages across recorded attempts.</span></div><span class="caption" id="coverageCount">Loading…</span></div>
        <div class="coverage-toolbar"><div class="field"><label for="coverageHost">Host</label><select id="coverageHost"><option value="">All hosts</option></select></div><div class="field"><label for="coverageTest">Test</label><select id="coverageTest"><option value="">All tests</option></select></div><div class="field"><label for="coverageStatus">Coverage state</label><select id="coverageStatus"><option value="">All states</option><option value="complete">Complete</option><option value="attention">Complete + retry needed</option><option value="incomplete">Incomplete / invalid</option><option value="missing">Not run yet</option><option value="skipped">Skipped by plan</option></select></div></div>
        <div class="coverage-summary" aria-label="Coverage legend"><span class="chip status-complete">Complete</span><span class="chip status-attention">Complete + retry</span><span class="chip status-incomplete">Incomplete / invalid</span><span class="chip status-missing">Not run yet</span><span class="chip status-skipped">Skipped by plan</span></div>
        <div class="coverage-wrap"><table class="coverage-table"><thead><tr><th>Host</th><th>Optimization</th><th>Test</th><th>State</th><th class="num">Result</th><th class="num">Energy</th><th class="num">Runs</th><th>Latest</th></tr></thead><tbody id="coverage"><tr><td colspan="8" class="loading">Loading test coverage…</td></tr></tbody></table></div>
      </section>
    </section>
    <section class="page-view" id="hostCompareView" hidden><section class="panel" aria-labelledby="hostCompareTitle"><div class="panel-head"><div><h2 id="hostCompareTitle">Configuration averages by host</h2><span class="caption">The primary chart groups each configuration’s average valid test value by host. Whiskers show the recorded min–max spread.</span></div></div><div class="host-compare-toolbar"><div class="field"><label for="hostCompareMetric">Measurement</label><select id="hostCompareMetric"><option value="energy">Integrated energy (Wh)</option><option value="idle">Idle power (W)</option><option value="power">Load power (W)</option><option value="score">Benchmark result</option></select></div><div class="field"><label for="hostCompareReference">Reference host</label><select id="hostCompareReference"></select></div><div class="field"><label for="hostCompareTest">Test</label><select id="hostCompareTest"></select></div><div class="field"><label for="hostCompareOptimization">Configuration</label><select id="hostCompareOptimization"></select></div></div><div class="host-charts"><section><p class="eyebrow">Primary metric</p><div class="host-chart"><canvas id="hostCompareAverage"></canvas></div></section><section><p class="eyebrow">Host delta</p><div class="host-chart"><canvas id="hostCompareDelta"></canvas></div></section></div></section></section>
  </main>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js"></script>
  <script>
    const state = { runs: [], coverage: [], hostComparisons: [], selected: null, filters: {} };
    const $ = (selector) => document.querySelector(selector);
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
    const number = (value, digits = 2) => value == null || Number.isNaN(Number(value)) ? '—' : Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
    const dateTime = (value) => { if (!value) return '—'; const parsed = new Date(String(value).replace(' ', 'T')); return Number.isNaN(parsed) ? String(value) : parsed.toLocaleString(undefined,{dateStyle:'medium',timeStyle:'short'}); };
    const shortTest = (name) => { if (!name) return 'untitled'; const last = String(name).split('/').filter(Boolean).pop(); return last || name; };
    const api = async (path) => { const response = await fetch(path, {cache:'no-store'}); const body = await response.json().catch(() => ({})); if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`); return body; };
    const query = () => { const p = new URLSearchParams(); const values = { optimization:$('#optimization').value, test:$('#test').value, host:$('#host').value, status:$('#status').value, q:$('#search').value.trim() }; Object.entries(values).forEach(([k,v]) => {if(v && !(k==='status' && v==='all'))p.set(k,v)}); return p.toString(); };
    const quality = (run) => { if (run.test === 'idle' && run.dropped_packets === 0) return ['idle captured','good']; if (!run.bench_end) return ['in progress','warn']; if (run.dropped_packets > 0) return [`${run.dropped_packets} dropped`,'bad']; if (run.bench_sample_coverage != null && run.bench_sample_coverage < .9) return [`${Math.round(run.bench_sample_coverage*100)}% coverage`,'warn']; return ['complete','good']; };
    function fillSelect(id, values, label) { const select = $(id); const prior = select.value; select.innerHTML = `<option value="">${label}</option>` + values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join(''); select.value = values.includes(prior) ? prior : ''; }
    async function loadOverview() { const overview = await api('/api/overview'); if (!overview.available) throw new Error(overview.reason || 'Database unavailable.'); $('#runCount').textContent = number(overview.run_count,0); $('#completeCount').textContent = number(overview.completed_count,0); $('#optimizationCount').textContent = number(overview.optimization_count,0); $('#readingCount').textContent = number(overview.reading_count,0); $('#latestRun').textContent = overview.latest_started_at ? dateTime(overview.latest_started_at) : 'No runs'; }
    async function loadFilters() { const filters = await api('/api/filters'); fillSelect('#optimization', filters.optimizations, 'All optimizations'); fillSelect('#test', filters.tests, 'All tests'); fillSelect('#host', filters.hosts, 'All hosts'); }
    function renderRuns(runs) { state.runs = runs; $('#resultCount').textContent = `${runs.length} run${runs.length===1?'':'s'} shown`; const tbody = $('#runs'); if (!runs.length) { tbody.innerHTML = '<tr><td colspan="5" class="loading">No runs match these filters.</td></tr>'; return; } tbody.innerHTML = runs.map(run => { const [q,kind] = quality(run); const result = run.bench_score == null ? '—' : `${number(run.bench_score,3)}${run.bench_unit ? ` ${escapeHtml(run.bench_unit)}` : ''}`; return `<tr class="run ${state.selected===run.run_id?'selected':''}" data-id="${run.run_id}" tabindex="0"><td><strong>#${run.run_id} · ${escapeHtml(shortTest(run.test))}</strong><span class="secondary">${escapeHtml(dateTime(run.started_at))} · repeat ${run.repeat_idx ?? '—'}</span></td><td><span class="run-name" title="${escapeHtml(run.optimization || '')}">${escapeHtml(run.optimization || 'baseline')}</span><span class="secondary">${escapeHtml(run.host || 'unknown host')}</span></td><td class="num">${result}</td><td class="num">${run.energy_wh_integrated == null ? '—' : `${number(run.energy_wh_integrated,3)} Wh`}</td><td><span class="chip ${kind}">${q}</span></td></tr>`; }).join(''); tbody.querySelectorAll('tr.run').forEach(row => { row.addEventListener('click', () => selectRun(Number(row.dataset.id))); row.addEventListener('keydown', event => { if(event.key==='Enter'||event.key===' '){event.preventDefault();selectRun(Number(row.dataset.id));} }); }); }
    async function loadRuns() { const qs = query(); const runs = await api('/api/runs' + (qs ? `?${qs}` : '')); renderRuns(runs); if (state.selected && !runs.some(run => run.run_id === state.selected)) clearDetail(); }
    async function loadComparison() { const qs = query(); const items = await api('/api/aggregates' + (qs ? `?${qs}` : '')); const target = $('#comparison'); const usable = items.filter(item => item.average_energy_wh != null); if (!usable.length) { target.innerHTML = '<span class="loading">No completed runs with energy data for this filter.</span>'; return; } const maximum = Math.max(...usable.map(item => Number(item.average_energy_wh))); target.innerHTML = usable.map(item => { const label = `${item.optimization || 'baseline'} · ${shortTest(item.test)}`; const width = Math.max(2, (Number(item.average_energy_wh) / maximum) * 100); return `<div class="compare-row"><span class="compare-label" title="${escapeHtml(label)}">${escapeHtml(label)}</span><div class="bar-track"><div class="bar" style="width:${width}%"></div></div><span class="bar-value">${number(item.average_energy_wh,3)} Wh <small>n=${item.run_count}</small></span></div>`; }).join(''); }
    const coverageLabel = (status) => ({complete:'Complete',attention:'Complete + retry',incomplete:'Incomplete / invalid',missing:'Not run yet',skipped:'Skipped by plan'})[status] || status;
    function coverageRows() { return state.coverage.filter(row => (!$('#coverageHost').value || row.host === $('#coverageHost').value) && (!$('#coverageTest').value || row.test === $('#coverageTest').value) && (!$('#coverageStatus').value || row.status === $('#coverageStatus').value)); }
    function coverageResult(row) { if (row.average_bench_score != null) return `${number(row.average_bench_score,3)}${row.bench_unit ? ` ${escapeHtml(row.bench_unit)}` : ''}`; if (row.average_idle_power_w != null) return `idle ${number(row.average_idle_power_w,2)} W`; return '—'; }
    function renderCoverage() { const rows = coverageRows(); const counts = rows.reduce((all,row) => ({...all,[row.status]:(all[row.status] || 0) + 1}), {}); $('#coverageCount').textContent = `${rows.length} combinations · ${counts.complete || 0} complete · ${counts.missing || 0} not run`; const tbody = $('#coverage'); if (!rows.length) { tbody.innerHTML = '<tr><td colspan="8" class="loading">No coverage combinations match these filters.</td></tr>'; return; } tbody.innerHTML = rows.map(row => { const runs = row.run_count ? `${row.valid_count}/${row.run_count} valid` : '—'; const latest = row.latest_run_id == null ? '—' : `<button class="open-run" type="button" data-id="${row.latest_run_id}">#${row.latest_run_id} · ${escapeHtml(dateTime(row.latest_started_at))}</button>`; return `<tr><td>${escapeHtml(row.host)}</td><td class="optimization-name">${escapeHtml(row.optimization)}</td><td class="test-name">${escapeHtml(row.test)}</td><td><span class="chip status-${escapeHtml(row.status)}">${escapeHtml(coverageLabel(row.status))}</span></td><td class="num result">${coverageResult(row)}</td><td class="num">${row.average_energy_wh == null ? '—' : `${number(row.average_energy_wh,3)} Wh`}</td><td class="num">${runs}</td><td class="latest">${latest}</td></tr>`; }).join(''); tbody.querySelectorAll('.open-run').forEach(button => button.addEventListener('click', () => { setView('explorer'); selectRun(Number(button.dataset.id)); })); }
    async function loadCoverage() { const coverage = await api('/api/coverage'); state.coverage = coverage; fillSelect('#coverageHost', [...new Set(coverage.map(row => row.host))], 'All hosts'); fillSelect('#coverageTest', [...new Set(coverage.map(row => row.test))], 'All tests'); renderCoverage(); }
    function hostCompareRows(){const fields={energy:['average_energy_wh','minimum_energy_wh','maximum_energy_wh'],idle:['average_idle_power_w','minimum_idle_power_w','maximum_idle_power_w'],power:['average_bench_power_w','minimum_bench_power_w','maximum_bench_power_w'],score:['average_bench_score','minimum_bench_score','maximum_bench_score']}[$('#hostCompareMetric').value],test=$('#hostCompareTest').value,optimization=$('#hostCompareOptimization').value;return state.hostComparisons.filter(r=>Number(r.valid_count)>0&&r[fields[0]]!=null&&(!test||r.test===test)&&(!optimization||r.optimization===optimization)).map(r=>({...r,mean:Number(r[fields[0]]),min:Number(r[fields[1]]),max:Number(r[fields[2]])})).sort((a,b)=>a.test.localeCompare(b.test)||a.optimization.localeCompare(b.optimization)||a.host.localeCompare(b.host))}
    const hostComparisonCharts={},hostRangeBars={id:'hostRangeBars',afterDatasetsDraw(chart){const y=chart.scales.y,ctx=chart.ctx;chart.data.datasets.forEach((dataset,d)=>chart.getDatasetMeta(d).data.forEach((bar,i)=>{const raw=dataset.data[i];if(!bar||!raw||!Number.isFinite(Number(raw.min))||!Number.isFinite(Number(raw.max))||!Number.isFinite(bar.x))return;const top=y.getPixelForValue(raw.max),bottom=y.getPixelForValue(raw.min);ctx.save();ctx.strokeStyle=dataset.borderColor;ctx.lineWidth=1.4;ctx.beginPath();ctx.moveTo(bar.x,top);ctx.lineTo(bar.x,bottom);ctx.moveTo(bar.x-4,top);ctx.lineTo(bar.x+4,top);ctx.moveTo(bar.x-4,bottom);ctx.lineTo(bar.x+4,bottom);ctx.stroke();ctx.restore()}))}};
    function drawHostComparison(id,config){if(!window.Chart){$('#'+id).parentElement.innerHTML='<div class="empty">Chart.js could not be loaded.</div>';return}if(hostComparisonCharts[id])hostComparisonCharts[id].destroy();hostComparisonCharts[id]=new Chart($('#'+id),config)}
    function renderHostComparisons(){const rows=hostCompareRows(),reference=$('#hostCompareReference').value,cohorts=Array.from(new Map(rows.map(r=>[[r.test,r.optimization].join('\0'),{test:r.test,optimization:r.optimization}])).values()),hosts=Array.from(new Set(rows.map(r=>r.host))).sort(),lookup=new Map(rows.map(r=>[[r.test,r.optimization,r.host].join('\0'),r])),labels=cohorts.map(c=>shortTest(c.test)+' · '+c.optimization),metric=$('#hostCompareMetric').selectedOptions[0].textContent,color=i=>['#087e78','#173f53','#e99c28','#c94e56','#7b5bb8'][i%5],datasets=hosts.map((h,i)=>({label:h,data:cohorts.map(c=>{const r=lookup.get([c.test,c.optimization,h].join('\0'));return r?{y:r.mean,min:r.min,max:r.max,n:r.valid_count}:null}),backgroundColor:color(i)+'99',borderColor:color(i),borderWidth:1})),averageLabel=c=>{const r=c.raw;return r?c.dataset.label+': '+number(r.y,3)+' (range '+number(r.min,3)+'–'+number(r.max,3)+', n='+r.n+')':''},averageConfig={type:'bar',data:{labels,datasets},plugins:[hostRangeBars],options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},scales:{x:{ticks:{maxRotation:45,minRotation:0}},y:{title:{display:true,text:metric},beginAtZero:false}},plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:averageLabel}}}}};drawHostComparison('hostCompareAverage',averageConfig);const deltaSets=hosts.filter(h=>h!==reference).map((h,i)=>({label:h,data:cohorts.map(c=>{const r=lookup.get([c.test,c.optimization,h].join('\0')),base=lookup.get([c.test,c.optimization,reference].join('\0'));return r&&base&&base.mean?{y:(r.mean/base.mean-1)*100,n:r.valid_count}:null}),backgroundColor:color(i)+'99',borderColor:color(i),borderWidth:1})),deltaLabel=c=>{const r=c.raw;return r?c.dataset.label+': '+(r.y>=0?'+':'')+number(r.y,1)+'% (n='+r.n+')':''},deltaConfig={type:'bar',data:{labels,datasets:deltaSets},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},scales:{x:{ticks:{maxRotation:45,minRotation:0}},y:{title:{display:true,text:'Delta (%)'},ticks:{callback:v=>(v>0?'+':'')+v+'%'},grid:{color:c=>c.tick.value===0?'#607070':'#d8e0dc'}}},plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:deltaLabel}}}}};drawHostComparison('hostCompareDelta',deltaConfig)}
    async function loadHostComparisons(){const rows=await api('/api/host-comparisons');state.hostComparisons=rows;fillSelect('#hostCompareReference',[...new Set(rows.map(r=>r.host))],'Reference host');fillSelect('#hostCompareTest',[...new Set(rows.map(r=>r.test))],'All tests');fillSelect('#hostCompareOptimization',[...new Set(rows.map(r=>r.optimization))],'All optimizations');if(!$('#hostCompareReference').value&&rows.length)$('#hostCompareReference').value=rows[0].host;renderHostComparisons()}
    function setView(view) { ['explorer','hostCompare','coverage'].forEach(name=>{const active=name===view;$(`#${name}View`).hidden=!active;$(`#${name}Tab`).setAttribute('aria-selected',String(active));}); }
    function displayValue(value) { if (value === null || value === undefined || value === '') return '—'; if (typeof value === 'boolean') return value ? 'yes' : 'no'; if (typeof value === 'number') return number(value, 4); return String(value); }
    function propertyRows(values) { return Object.entries(values).filter(([,value]) => value !== null && value !== undefined && value !== '').map(([key,value]) => `<div class="property"><dt>${escapeHtml(key.replaceAll('_',' '))}</dt><dd class="${String(value).length>44?'long':''}">${escapeHtml(displayValue(value))}</dd></div>`).join('') || '<span class="caption">No values recorded.</span>'; }
    function renderDetail(run) { state.selected = run.run_id; $('#emptyDetail').style.display = 'none'; const detail = $('#detail'); const config = run.applied_config_parsed || {}; const variables = { host:run.host, test:run.test, optimization:run.optimization, repeat:run.repeat_idx, started_at:dateTime(run.started_at), config_hash:run.config_hash, kernel:run.kernel, cpu_model:run.cpu_model, governor:run.governor, turbo:run.turbo, ambient_c:run.ambient_c == null ? null : `${number(run.ambient_c,1)} °C`, bench_start_temp_c:run.bench_start_temp_c == null ? null : `${number(run.bench_start_temp_c,1)} °C`, dropped_packets:run.dropped_packets, checksum_failures:run.checksum_failures, coverage:run.bench_sample_coverage == null ? null : `${number(run.bench_sample_coverage*100,1)}%` };
      const resultRows = run.results.length ? run.results.map(result => `<tr><td>${escapeHtml(result.title)}</td><td>${number(result.value,4)}</td><td>${escapeHtml(result.scale || '—')}</td><td>${result.higher_is_better == null ? '—' : result.higher_is_better ? 'higher is better' : 'lower is better'}</td></tr>`).join('') : '<tr><td colspan="4" class="caption">No separate PTS result entries stored for this run.</td></tr>';
      detail.innerHTML = `<div class="run-title"><span class="eyebrow">Run #${run.run_id}</span><h2>${escapeHtml(run.optimization || 'Baseline')}</h2><div class="meta">${escapeHtml(shortTest(run.test))} · ${escapeHtml(dateTime(run.started_at))}</div></div><div class="metrics"><div class="metric"><small>Primary result</small><strong>${run.bench_score == null ? '—' : number(run.bench_score,3)}</strong><span class="caption">${escapeHtml(run.bench_unit || 'not recorded')}</span></div><div class="metric"><small>Integrated energy</small><strong>${run.energy_wh_integrated == null ? '—' : `${number(run.energy_wh_integrated,3)} Wh`}</strong><span class="caption">bench interval</span></div><div class="metric"><small>Sample coverage</small><strong>${run.bench_sample_coverage == null ? '—' : `${number(run.bench_sample_coverage*100,1)}%`}</strong><span class="caption">${run.bench_end ? 'completed run' : 'in progress / incomplete'}</span></div></div><section class="detail-section"><h3>Power by phase</h3><div class="phase-grid">${run.phase_stats.map(phase => `<div class="phase ${escapeHtml(phase.phase || '')}"><span class="phase-name">${escapeHtml(phase.phase || 'unassigned')} · ${number(phase.samples,0)} samples</span><strong>${number(phase.average_power_w,2)} W</strong><small>${number(phase.min_power_w,2)}–${number(phase.max_power_w,2)} W · ${number(phase.min_temperature_c,1)}–${number(phase.max_temperature_c,1)} °C</small></div>`).join('') || '<span class="caption">No captured power samples for this run.</span>'}</div></section><section class="detail-section"><h3>Power trace</h3><canvas id="plot" aria-label="Power over time"></canvas><div class="legend"><span class="key"><i class="dot"></i>settle / other</span><span class="key"><i class="dot idle"></i>idle</span><span class="key"><i class="dot bench"></i>bench</span><span class="key"><i class="dot cooldown"></i>cooldown</span><span class="caption">${run.samples.length} sampled points</span></div></section><section class="detail-section"><h3>Captured run variables</h3><dl class="properties">${propertyRows(variables)}</dl></section><section class="detail-section"><h3>Applied configuration</h3><dl class="properties">${propertyRows(config)}</dl></section><section class="detail-section"><h3>All benchmark results</h3><table class="results-table"><thead><tr><th>Title</th><th>Value</th><th>Scale</th><th>Direction</th></tr></thead><tbody>${resultRows}</tbody></table></section>`;
      detail.classList.add('visible'); drawPlot(run.samples); renderRuns(state.runs); }
    function clearDetail() { state.selected = null; $('#detail').classList.remove('visible'); $('#detail').innerHTML = ''; $('#emptyDetail').style.display = ''; renderRuns(state.runs); }
    function drawPlot(samples) { const canvas = $('#plot'); if (!canvas || !samples.length) return; const box = canvas.getBoundingClientRect(); const ratio = Math.min(window.devicePixelRatio || 1, 2); canvas.width = Math.floor(box.width * ratio); canvas.height = Math.floor(box.height * ratio); const ctx = canvas.getContext('2d'); ctx.scale(ratio,ratio); const width = box.width, height = box.height; const pad={top:16,right:12,bottom:25,left:43}; const powers=samples.map(s=>Number(s.power_w)).filter(Number.isFinite); if(!powers.length)return; let min=Math.min(...powers),max=Math.max(...powers); const margin=Math.max(.3,(max-min)*.1); min=Math.max(0,min-margin);max+=margin; const start=Number(samples[0].timestamp), end=Number(samples[samples.length-1].timestamp), span=Math.max(1,end-start); const x=s=>pad.left+((Number(s.timestamp)-start)/span)*(width-pad.left-pad.right); const y=s=>pad.top+(max-Number(s.power_w))/(max-min)*(height-pad.top-pad.bottom); ctx.clearRect(0,0,width,height); ctx.font='10px system-ui';ctx.fillStyle='#62716d';ctx.strokeStyle='#dfe6df';ctx.lineWidth=1; for(let i=0;i<4;i++){const yy=pad.top+(height-pad.top-pad.bottom)*i/3;ctx.beginPath();ctx.moveTo(pad.left,yy);ctx.lineTo(width-pad.right,yy);ctx.stroke();ctx.fillText(`${(max-(max-min)*i/3).toFixed(1)} W`,2,yy+3);} const colors={idle:'#a1c42b',bench:'#e99c28',cooldown:'#173f53',settle:'#087e78'}; let previous=null; samples.forEach(sample=>{if(!Number.isFinite(Number(sample.power_w)))return;ctx.strokeStyle=colors[sample.phase]||'#087e78';ctx.lineWidth=1.55;ctx.beginPath();if(previous&&previous.phase===sample.phase)ctx.moveTo(x(previous),y(previous));else ctx.moveTo(x(sample),y(sample));ctx.lineTo(x(sample),y(sample));ctx.stroke();previous=sample;});ctx.fillStyle='#62716d';ctx.fillText('start',pad.left,height-7);ctx.textAlign='right';ctx.fillText(`${Math.round(span)} s`,width-pad.right,height-7);ctx.textAlign='left'; }
    async function selectRun(runId) { $('#detail').innerHTML = '<div class="empty"><div class="loading">Loading run details…</div></div>'; $('#detail').classList.add('visible'); $('#emptyDetail').style.display='none'; try { renderDetail(await api(`/api/runs/${runId}`)); } catch(error) { $('#detail').innerHTML = `<div class="error">${escapeHtml(error.message)}</div>`; } }
    let searchTimer;
    function refreshRuns() { Promise.all([loadRuns(),loadComparison()]).catch(showError); }
    function showError(error) { const message = error.message || 'Unexpected error.'; $('#runs').innerHTML = `<tr><td colspan="5"><div class="error">${escapeHtml(message)}</div></td></tr>`; $('#comparison').innerHTML = `<div class="error">${escapeHtml(message)}</div>`; }
    async function start() { try { await Promise.all([loadOverview(),loadFilters(),loadCoverage(),loadHostComparisons()]); await Promise.all([loadRuns(),loadComparison()]); } catch(error) { showError(error); } }
    ['#optimization','#test','#host','#status'].forEach(id=>$(id).addEventListener('change',refreshRuns)); ['#coverageHost','#coverageTest','#coverageStatus'].forEach(id=>$(id).addEventListener('change',renderCoverage)); ['#hostCompareMetric','#hostCompareReference','#hostCompareTest','#hostCompareOptimization'].forEach(id=>$(id).addEventListener('change',renderHostComparisons)); $('#search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(refreshRuns,180)}); $('#clearFilters').addEventListener('click',()=>{['#optimization','#test','#host','#search'].forEach(id=>$(id).value='');$('#status').value='all';refreshRuns();}); $('#explorerTab').addEventListener('click',()=>setView('explorer')); $('#hostCompareTab').addEventListener('click',()=>setView('hostCompare')); $('#coverageTab').addEventListener('click',()=>setView('coverage')); $('#refresh').addEventListener('click',()=>start()); window.addEventListener('resize',()=>{if(state.selected){const run = null; /* canvas is redrawn when another run is selected */}}); start();
  </script>
</body>
</html>'''


def existing_database(path: Path) -> Path:
    """Prefer the populated benchmark store but fall back to the root database."""
    if path.exists():
        return path
    fallback = ROOT / "power_meter.duckdb"
    if path == DEFAULT_DB and fallback.exists():
        return fallback
    raise FileNotFoundError(f"Database not found: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve an interactive, read-only power-benchmark dashboard.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"DuckDB database to read (default: {DEFAULT_DB.relative_to(ROOT)})")
    parser.add_argument("--host", default="0.0.0.0", help="Address to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="TCP port to bind (default: 8080)")
    args = parser.parse_args()
    try:
        database = existing_database(args.db.expanduser().resolve())
    except FileNotFoundError as error:
        parser.error(str(error))
    # A quick connection yields a clear startup failure before a browser is opened.
    try:
        with duckdb.connect(str(database), read_only=True) as conn:
            conn.execute("SELECT 1")
    except (duckdb.Error, OSError, sqlite3.Error) as error:
        parser.error(f"Could not open {database} read-only: {error}")
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    server.reader = DatabaseReader(database)  # type: ignore[attr-defined]
    print(f"Power Bench dashboard: http://{args.host}:{args.port}")
    print(f"Reading: {database}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
