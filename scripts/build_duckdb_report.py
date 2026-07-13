#!/usr/bin/env python3
"""Render a DuckDB database as a standalone HTML report."""

from __future__ import annotations

import argparse
from datetime import date, datetime, time
from decimal import Decimal
from html import escape
import json
from pathlib import Path
from typing import Any

import duckdb


DEFAULT_ROW_LIMIT = 250


def quote_identifier(identifier: str) -> str:
    return f'"{identifier.replace("\"", "\"\"")}"'


def display_value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bytes):
        return f"{len(value)} bytes"
    if isinstance(value, (date, datetime, time, Decimal)):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def table_names(connection: duckdb.DuckDBPyConnection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
    ]


def table_section(
    connection: duckdb.DuckDBPyConnection, table: str, row_limit: int
) -> tuple[int, str]:
    quoted_table = quote_identifier(table)
    columns = [
        row[1]
        for row in connection.execute(f"PRAGMA table_info({quoted_table})").fetchall()
    ]
    row_count = connection.execute(f"SELECT count(*) FROM {quoted_table}").fetchone()[0]
    order_column = "run_id" if "run_id" in columns else columns[0] if columns else None
    query = f"SELECT * FROM {quoted_table}"
    if order_column:
        query += f" ORDER BY {quote_identifier(order_column)} DESC NULLS LAST"
    query += " LIMIT ?"
    rows = connection.execute(query, [row_limit]).fetchall()

    heading = escape(table)
    details = f"{row_count:,} row{'s' if row_count != 1 else ''}"
    if row_count > len(rows):
        details += f"; showing the latest {len(rows):,}"

    rendered_rows = "".join(
        "<tr>"
        + "".join(f"<td>{escape(display_value(value))}</td>" for value in row)
        + "</tr>"
        for row in rows
    )
    if not rendered_rows:
        rendered_rows = f'<tr><td colspan="{max(1, len(columns))}">No rows.</td></tr>'

    rendered_columns = "".join(f"<th>{escape(column)}</th>" for column in columns)
    return row_count, f"""
    <section id="{heading}">
      <h2>{heading}</h2>
      <p class="table-summary">{details}</p>
      <div class="table-wrap">
        <table>
          <thead><tr>{rendered_columns}</tr></thead>
          <tbody>{rendered_rows}</tbody>
        </table>
      </div>
    </section>
    """


def render_report(database: Path, output: Path, row_limit: int) -> None:
    with duckdb.connect(str(database), read_only=True) as connection:
        tables = table_names(connection)
        sections: list[str] = []
        total_rows = 0
        for table in tables:
            row_count, section = table_section(connection, table, row_limit)
            total_rows += row_count
            sections.append(section)

    table_links = "".join(
        f'<li><a href="#{escape(table)}">{escape(table)}</a></li>' for table in tables
    )
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    report = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Benchmark data report</title>
  <style>
    :root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
    body {{ margin: 0; color: #1f2933; background: #f7fafc; }}
    header, main {{ max-width: 1200px; margin: auto; padding: 1.5rem; }}
    header {{ max-width: none; color: white; background: #155799; }}
    header > div {{ max-width: 1200px; margin: auto; }}
    h1 {{ margin: 0; }}
    h2 {{ margin-top: 2.5rem; }}
    a {{ color: #155799; }}
    header a {{ color: white; }}
    .summary, .table-summary {{ color: #52606d; }}
    .table-wrap {{ overflow-x: auto; background: white; border: 1px solid #d9e2ec; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
    th, td {{ padding: .6rem .75rem; text-align: left; vertical-align: top; border-bottom: 1px solid #e4e7eb; }}
    th {{ position: sticky; top: 0; background: #edf2f7; white-space: nowrap; }}
    td {{ max-width: 28rem; overflow-wrap: anywhere; }}
    @media (prefers-color-scheme: dark) {{
      body, .table-wrap {{ color: #e6edf3; background: #0d1117; }}
      .summary, .table-summary {{ color: #b1bac4; }}
      th {{ background: #161b22; }}
      th, td, .table-wrap {{ border-color: #30363d; }}
      a {{ color: #79c0ff; }}
      header a {{ color: white; }}
    }}
  </style>
</head>
<body>
  <header><div><h1>Benchmark data report</h1><p><a href="./">Read the analysis report</a></p></div></header>
  <main>
    <p class="summary">Generated from <code>{escape(database.as_posix())}</code> on {escape(generated_at)}. {len(tables):,} tables and {total_rows:,} rows.</p>
    <nav aria-label="Database tables"><ul>{table_links}</ul></nav>
    {''.join(sections)}
  </main>
</body>
</html>
"""
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
