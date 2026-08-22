"""Export benchmark data from power_meter.duckdb to site/data.json for the SPA report.

Usage: uv run python scripts/export_report_data.py [--db PATH] [--out PATH]
Self-check: uv run python scripts/export_report_data.py --check
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Canonical configuration spectrum: 0 = max performance ... 100 = max powersave.
# Ordered most-powersave-first so substring matches resolve correctly
# ("power" must win before "performance" sees "balance_performance").
SCALE = [
    (100, "Max powersave", ("max_powersave", "epp=power", "max power")),
    (75, "Powersave", ("powersave", "balance_power", "conservative", "idle")),
    (25, "Performance", ("balance_performance",)),
    (0, "Max performance", ("max_performance", "performance", "baseline")),
]
PHASES = ["settle", "idle", "bench", "cooldown"]


def map_scale(optimization: str | None) -> int | None:
    """Map an optimization/config name onto the 0/25/75/100 spectrum (None = unknown)."""
    if not optimization:
        return None
    norm = optimization.strip().lower()
    for value, _label, keys in SCALE:
        if any(k in norm for k in keys):
            return value
    return None


def export(db_path: str) -> dict:
    con = duckdb.connect(db_path, read_only=True)
    cols = [r[0] for r in con.execute(
        "select column_name from information_schema.columns where table_name='runs'"
    ).fetchall()]
    runs = [dict(zip(cols, row)) for row in con.execute(
        f"SELECT {', '.join(cols)} FROM runs ORDER BY run_id"
    ).fetchall()]
    for run in runs:
        run["scale"] = map_scale(run.get("optimization"))

    readings = {}
    try:
        for run_id, ts, phase, power in con.execute(
            "SELECT run_id, timestamp, phase, power_w FROM readings "
            "WHERE run_id IS NOT NULL ORDER BY run_id, timestamp"
        ).fetchall():
            entry = readings.setdefault(str(run_id), {"t": [], "p": [], "ph": []})
            entry["t"].append(round(ts, 2))
            entry["p"].append(power)
            entry["ph"].append(PHASES.index(phase) if phase in PHASES else -1)
    except duckdb.BinderException:
        pass  # legacy DB without run_id/phase columns
    con.close()
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scale": [{"value": v, "label": l} for v, l, _ in SCALE],
        "runs": runs,
        "readings": readings,
    }


def _check():
    assert map_scale("epp=performance") == 0
    assert map_scale("baseline") == 0
    assert map_scale("epp=balance_performance") == 25
    assert map_scale("cpu_governor=powersave") == 75
    assert map_scale("pcie_aspm=powersave") == 75
    assert map_scale("epp=power") == 100
    assert map_scale("combined=turbo_off+governor_powersave") == 75
    assert map_scale(None) is None
    assert map_scale("totally_unknown") is None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="power_meter.duckdb")
    ap.add_argument("--out", default="site/data.json")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        _check()
        print("ok")
        return
    data = export(args.db)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, default=str))  # TIMESTAMP cols -> str
    print(f"{len(data['runs'])} runs, {sum(len(r['t']) for r in data['readings'].values())} "
          f"readings -> {out}")


if __name__ == "__main__":
    main()
