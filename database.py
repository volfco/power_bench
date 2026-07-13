"""DuckDB storage layer for power-meter readings and benchmark runs.

A single database holds three tables:

* ``runs``        — one row per benchmark invocation: metadata, phase boundaries,
                    integrated energy, energy-counter snapshots and the retrieved
                    Phoronix score.
* ``readings``    — power samples, each tagged with its ``run_id`` and ``phase``
                    ('settle' | 'idle' | 'bench' | 'cooldown') so idle vs. load
                    queries are exact and exclude post-boot settling.
* ``run_results`` — every PTS <Result> entry for a run (multi-result profiles and
                    suites); ``runs.bench_score`` keeps the first/primary one.
"""

import os

import duckdb
from atorch_protocol import MeterReading

CREATE_READINGS_SEQ = "CREATE SEQUENCE IF NOT EXISTS readings_seq START 1"
CREATE_RUNS_SEQ = "CREATE SEQUENCE IF NOT EXISTS runs_seq START 1"

CREATE_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id                INTEGER DEFAULT (nextval('runs_seq')) PRIMARY KEY,
    started_at            TIMESTAMP NOT NULL DEFAULT (current_timestamp),
    host                  VARCHAR,
    test                  VARCHAR,
    optimization          VARCHAR,
    repeat_idx            INTEGER,
    config_hash           VARCHAR,
    kernel                VARCHAR,
    cpu_model             VARCHAR,
    memory_bytes          BIGINT,
    governor              VARCHAR,
    turbo                 VARCHAR,
    ambient_c             DOUBLE,
    applied_config        VARCHAR,
    bench_start_temp_c    DOUBLE,
    result_name           VARCHAR,
    idle_start            DOUBLE,
    bench_start           DOUBLE,
    bench_end             DOUBLE,
    energy_wh_integrated  DOUBLE,
    energy_wh_bench_start DOUBLE,
    energy_wh_bench_end   DOUBLE,
    bench_score           DOUBLE,
    bench_unit            VARCHAR,
    higher_is_better      BOOLEAN,
    dropped_packets       INTEGER,
    checksum_failures     INTEGER,
    bench_sample_coverage DOUBLE
)
"""

CREATE_RUN_RESULTS_SQL = """
CREATE TABLE IF NOT EXISTS run_results (
    run_id           INTEGER,
    title            VARCHAR,
    scale            VARCHAR,
    higher_is_better BOOLEAN,
    value            DOUBLE
)
"""

CREATE_READINGS_SQL = """
CREATE TABLE IF NOT EXISTS readings (
    id              INTEGER DEFAULT (nextval('readings_seq')) PRIMARY KEY,
    run_id          INTEGER,
    phase           VARCHAR,
    timestamp       DOUBLE NOT NULL,
    capture_time    TIMESTAMP NOT NULL DEFAULT (current_timestamp),
    device_type     VARCHAR NOT NULL,
    voltage_v       DOUBLE,
    current_a       DOUBLE,
    power_w         DOUBLE,
    capacity_mah    DOUBLE,
    energy_wh       DOUBLE,
    temperature_c   DOUBLE,
    duration_h      INTEGER,
    duration_m      INTEGER,
    duration_s      INTEGER,
    usb_d_minus_v   DOUBLE,
    usb_d_plus_v    DOUBLE,
    frequency_hz    DOUBLE,
    power_factor    DOUBLE,
    price_per_kwh   DOUBLE
)
"""

# Bring pre-existing tables (created before newer columns existed) up to date.
MIGRATIONS = [
    "ALTER TABLE readings ADD COLUMN IF NOT EXISTS run_id INTEGER",
    "ALTER TABLE readings ADD COLUMN IF NOT EXISTS phase VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS applied_config VARCHAR",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS memory_bytes BIGINT",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS bench_start_temp_c DOUBLE",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS energy_wh_integrated DOUBLE",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS bench_sample_coverage DOUBLE",
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS checksum_failures INTEGER",
]

# Columns that create_run()/update_run() are allowed to write (guards against
# accidental SQL injection via keyword names).
_RUN_COLUMNS = {
    "host", "test", "optimization", "repeat_idx", "config_hash",
    "kernel", "cpu_model", "memory_bytes", "governor", "turbo", "ambient_c", "result_name",
    "applied_config", "bench_start_temp_c",
    "idle_start", "bench_start", "bench_end",
    "energy_wh_integrated", "energy_wh_bench_start", "energy_wh_bench_end",
    "bench_score", "bench_unit", "higher_is_better", "dropped_packets",
    "checksum_failures", "bench_sample_coverage",
}


class Database:
    def __init__(self, path: str = "power_meter.duckdb"):
        self.path = path
        self._conn: duckdb.DuckDBPyConnection | None = None

    def open(self):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = duckdb.connect(self.path)
        self._conn.execute(CREATE_READINGS_SEQ)
        self._conn.execute(CREATE_RUNS_SEQ)
        self._conn.execute(CREATE_RUNS_SQL)
        self._conn.execute(CREATE_READINGS_SQL)
        self._conn.execute(CREATE_RUN_RESULTS_SQL)
        for stmt in MIGRATIONS:
            self._conn.execute(stmt)

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    def create_run(self, **fields) -> int:
        """Insert a run row and return its generated run_id."""
        cols = [c for c in fields if c in _RUN_COLUMNS]
        if not cols:
            row = self._conn.execute(
                "INSERT INTO runs DEFAULT VALUES RETURNING run_id"
            ).fetchone()
            return row[0]
        placeholders = ", ".join("?" for _ in cols)
        sql = (
            f"INSERT INTO runs ({', '.join(cols)}) "
            f"VALUES ({placeholders}) RETURNING run_id"
        )
        row = self._conn.execute(sql, [fields[c] for c in cols]).fetchone()
        return row[0]

    def update_run(self, run_id: int, **fields):
        cols = [c for c in fields if c in _RUN_COLUMNS]
        if not cols:
            return
        assignments = ", ".join(f"{c} = ?" for c in cols)
        params = [fields[c] for c in cols] + [run_id]
        self._conn.execute(
            f"UPDATE runs SET {assignments} WHERE run_id = ?", params
        )

    def insert(self, reading: MeterReading, run_id: int | None = None, phase: str | None = None):
        self._conn.execute(
            """
            INSERT INTO readings (
                run_id, phase, timestamp, device_type, voltage_v, current_a, power_w,
                capacity_mah, energy_wh, temperature_c,
                duration_h, duration_m, duration_s,
                usb_d_minus_v, usb_d_plus_v, frequency_hz, power_factor, price_per_kwh
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                phase,
                reading.timestamp,
                reading.device_type.name,
                reading.voltage,
                reading.current,
                reading.power,
                reading.capacity,
                reading.energy,
                reading.temperature,
                reading.duration_hours,
                reading.duration_minutes,
                reading.duration_seconds,
                reading.usb_d_minus,
                reading.usb_d_plus,
                reading.frequency,
                reading.power_factor,
                reading.price,
            ],
        )

    def insert_run_result(self, run_id: int, title: str, scale: str,
                          higher_is_better: bool, value: float):
        """Store one PTS <Result> entry; a run may have several (suites etc.)."""
        self._conn.execute(
            "INSERT INTO run_results (run_id, title, scale, higher_is_better, value) "
            "VALUES (?, ?, ?, ?, ?)",
            [run_id, title, scale, higher_is_better, value],
        )

    def query(self, sql: str, params=None):
        return self._conn.execute(sql, params or []).fetchall()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
