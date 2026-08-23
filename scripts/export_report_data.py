"""Export benchmark data from power_meter.duckdb to site/data.json for the SPA report.

Usage: uv run python scripts/export_report_data.py [--db PATH] [--out PATH]
Self-check: uv run python scripts/export_report_data.py --check
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Canonical configuration spectrum: 0 = max performance ... 100 = max powersave.
# Ordered most-powersave-first so substring matches resolve correctly
# ("power" must win before "performance" sees "balance_performance").
SCALE = [
    (100, "Max powersave", ("max_powersave", "epp=power", "max power", "supersave")),
    (75, "Powersave", ("powersave", "balance_power", "conservative", "idle")),
    (25, "Performance", ("balance_performance",)),
    (0, "Max performance", ("max_performance", "performance", "baseline")),
]
PHASES = ["settle", "idle", "bench", "cooldown"]
IDLE_PH, BENCH_PH = 1, 2
# Alias so the legacy-tolerance handler below reads as a plain name.
BinderException = duckdb.BinderException

# Bucket score weights: (performance weight, power weight) per scale point.
BUCKET_WEIGHTS = {0: (1.0, 0.0), 25: (0.75, 0.25), 75: (0.25, 0.75), 100: (0.0, 1.0)}
MIN_COVERAGE = 0.7


def map_scale(optimization: str | None) -> int | None:
    """Map an optimization/config name onto the 0/25/75/100 spectrum (None = unknown)."""
    if not optimization:
        return None
    norm = optimization.strip().lower()
    for value, _label, keys in SCALE:
        if any(k in norm for k in keys):
            return value
    return None


def percentile_rank(values: list[float], v: float) -> float:
    """Rank of v within values as a 0..1 fraction (mid-rank on ties)."""
    below = sum(1 for x in values if x < v)
    ties = sum(1 for x in values if x == v)
    return (below + ties / 2) / len(values)


def phase_stats(entry: dict | None) -> dict:
    """Idle/bench averages + peak power from a run's readings entry."""
    if not entry or not entry["p"]:
        return {"samples": 0, "idle_samples": 0, "bench_samples": 0,
                "idle_w": None, "load_w": None, "peak_w": None}
    p = entry["p"]
    idle = [x for x, ph in zip(p, entry["ph"]) if ph == IDLE_PH]
    bench = [x for x, ph in zip(p, entry["ph"]) if ph == BENCH_PH]
    def avg(xs: list[float]) -> float | None:
        return sum(xs) / len(xs) if xs else None
    idle_avg, bench_avg = avg(idle), avg(bench)
    return {"samples": len(p), "idle_samples": len(idle), "bench_samples": len(bench),
            "idle_w": round(idle_avg, 3) if idle_avg is not None else None,
            "load_w": round(bench_avg, 3) if bench_avg is not None else None,
            "peak_w": round(max(p), 3)}


def quality(run: dict, stats: dict) -> tuple[bool, str]:
    """Validity flag + human-readable reason (see data_report_todo.md section A)."""
    if run.get("invalid_reason"):
        return False, f"invalidated: {run['invalid_reason']}"
    dropped = run.get("dropped_packets") or 0
    if dropped:
        return False, f"{dropped} dropped packets"
    if (run.get("test") or "") == "idle":
        if not stats["idle_samples"]:
            return False, "no idle samples"
        return True, "valid idle capture"
    if not run.get("bench_end"):
        return False, "incomplete (no bench end)"
    if run.get("bench_score") is None:
        return False, "no bench result"
    cov = run.get("bench_sample_coverage")
    if cov is not None and cov < MIN_COVERAGE:
        return False, f"{round(cov * 100)}% sample coverage < {MIN_COVERAGE:.0%}"
    return True, "valid"


def enrich(runs: list[dict], readings: dict) -> None:
    """Attach derived per-run fields in place: energy fallback, phase power stats,
    load power, validity."""
    for run in runs:
        stats = phase_stats(readings.get(str(run["run_id"])))
        # DB columns win over readings-derived stats where both exist.
        for k, v in stats.items():
            if run.get(k) is None:
                run[k] = v
        # Energy fallback chain: integrated -> meter delta over the bench window.
        if run.get("energy_wh_integrated") is None and \
                run.get("energy_wh_bench_end") is not None and \
                run.get("energy_wh_bench_start") is not None:
            run["energy_wh"] = run["energy_wh_bench_end"] - run["energy_wh_bench_start"]
        else:
            run["energy_wh"] = run.get("energy_wh_integrated")
        dur = (run.get("bench_end") or 0) - (run.get("bench_start") or 0)
        if run["energy_wh"] is not None and dur > 0:
            run["load_w"] = round(run["energy_wh"] * 3600 / dur, 3)
        elif run.get("load_w") is None:
            run["load_w"] = stats["load_w"]
        valid, reason = quality(run, stats)
        run["valid"] = valid
        run["quality_reason"] = reason


def score_runs(runs: list[dict]) -> None:
    """Bucket-weighted scores, added to each run in place.

    Cohort = one host x test (never blended across unlike hosts). Metrics are
    percentile-ranked within the cohort against valid runs only, which makes the
    score baseline-normalized by construction; each bucket then blends its
    performance/power weights. Idle tests score the power side only.
    """
    cohorts: dict[tuple, list[dict]] = defaultdict(list)
    for r in runs:
        if r.get("valid"):
            cohorts[(r.get("host"), r.get("test"))].append(r)
    for members in cohorts.values():
        is_idle = (members[0].get("test") or "") == "idle"
        perf = {r["run_id"]: r["bench_score"] for r in members if r.get("bench_score") is not None}
        pwr = {r["run_id"]: (r.get("idle_w") if is_idle else r.get("load_w")) for r in members}
        pwr = {k: v for k, v in pwr.items() if v is not None}
        pv, wv = list(perf.values()), list(pwr.values())
        for r in members:
            # Idle tests score the power side only — no bucket needed, so runs
            # whose optimization has no scale mapping still get scored.
            if is_idle:
                wp, we = 0.0, 1.0
            else:
                weights = BUCKET_WEIGHTS.get(r.get("scale"))
                if weights is None:
                    continue
                wp, we = weights
            rid, total = r["run_id"], 0.0
            if wp > 0:
                v = perf.get(rid)
                if v is None:
                    continue
                hib = r.get("higher_is_better") in (None, True)
                pr = percentile_rank(pv, v)
                total += wp * (pr if hib else 1 - pr) * 100
            if we > 0:
                v = pwr.get(rid)
                if v is None:
                    continue
                total += we * (1 - percentile_rank(wv, v)) * 100
            r["score"] = round(total, 1)
    for r in runs:
        r.setdefault("score", None)


def architecture_map() -> dict[str, str]:
    """Config name -> Intel/AMD/Both, from the run_suite catalogs (empty on import failure)."""
    intel, amd = _catalogs()
    both = intel & amd
    return {**{n: "Both" for n in both}, **{n: "Intel" for n in intel - both},
            **{n: "AMD" for n in amd - both}}


def _catalogs() -> tuple[set[str], set[str]]:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from run_suite import AMD_EXPERIMENTS, EXPERIMENTS
    except ImportError:
        return set(), set()
    return ({label for label, _, _ in EXPERIMENTS}, {label for label, _, _ in AMD_EXPERIMENTS})


def experiment_plan() -> dict:
    """Suite catalog needed to classify planned vs skipped host x config x test combos."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from run_suite import EXPERIMENTS, IDLE_TEST, PERF_FLOOR_TEST
    except ImportError:
        return {"targets": {}, "idleTest": "idle", "perfFloorTest": None}
    return {"targets": {label: target for label, _, target in EXPERIMENTS},
            "idleTest": IDLE_TEST, "perfFloorTest": PERF_FLOOR_TEST}


def subtest_results(con) -> dict:
    """All suite sub-test results (run_results), titles de-duplicated into metadata."""
    try:
        rows = con.execute(
            "SELECT run_id, title, scale, higher_is_better, value FROM run_results ORDER BY run_id"
        ).fetchall()
    except duckdb.Error as exc:
        raise SystemExit(f"run_results unreadable: {exc}") from exc
    titles: dict[str, dict] = {}
    compact = []
    for run_id, title, scale, hib, value in rows:
        idx = list(titles).index(title) if title in titles else len(titles)
        if title not in titles:
            titles[title] = {"scale": scale, "hib": bool(hib)}
        if value is not None:
            compact.append([run_id, idx, round(value, 4)])
    return {"titles": titles, "rows": compact}


def raw_previews(con, limit: int = 250) -> dict:
    """Row counts + latest N rows (newest first) for every table (legacy tolerant)."""
    tables = [r[0] for r in con.execute(
        "select table_name from information_schema.tables where table_schema='main'"
    ).fetchall()]
    out = {}
    for t in tables:
        try:
            count = con.execute(f'select count(*) from "{t}"').fetchone()[0]
            order_col = "rowid"  # ponytail: rowid ~= insertion order; good enough for a preview
            cols = [r[0] for r in con.execute(
                f"select column_name from information_schema.columns where table_name='{t}'"
            ).fetchall()]
            rows = con.execute(
                f'select * from "{t}" order by {order_col} desc limit {limit}'
            ).fetchall()
        except duckdb.Error as exc:
            print(f"warning: skipping table {t}: {exc}")
            continue
        out[t] = {"count": count, "columns": cols,
                  "rows": [[str(v) for v in row] for row in rows]}
    return out


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
    except BinderException:
        pass  # legacy DB without run_id/phase columns
    subtests = subtest_results(con)
    raw = raw_previews(con)
    con.close()
    enrich(runs, readings)
    score_runs(runs)
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scale": [{"value": v, "label": l} for v, l, _ in SCALE],
        "arch": architecture_map(),
        "plan": experiment_plan(),
        "subtests": subtests,
        "runs": runs,
        "readings": readings,
        "_raw": raw,
    }


def _check():
    # --- scoring & quality model ---
    mk = lambda rid, host, test, scale, score, idle_w=None, load_w=None, **kw: {
        "run_id": rid, "host": host, "test": test, "scale": scale, "bench_score": score,
        "higher_is_better": True, "idle_w": idle_w, "load_w": load_w, "valid": True,
        "dropped_packets": 0, "invalid_reason": None, "bench_end": 10.0,
        "bench_sample_coverage": 1.0, **kw}
    runs = [
        mk(1, "h", "t", 0, 100, load_w=50),          # baseline fast+hungry
        mk(2, "h", "t", 100, 60, load_w=30),         # powersave slow+lean
        mk(3, "h", "t", 25, 90, load_w=45),
        mk(4, "h", "idle", 100, None, idle_w=10),    # idle runs: power only
        mk(5, "h", "idle", 0, None, idle_w=15),
        mk(6, "h", "t", 25, 95, load_w=44, valid=False, invalid_reason="manual"),
        mk(7, "h", "idle", None, None, idle_w=12),  # unmapped optimization: still scored
    ]
    readings = {
        "4": {"t": [0, 1], "p": [10.0, 10.5], "ph": [0, IDLE_PH]},
        "5": {"t": [0, 1], "p": [15.0, 15.2], "ph": [0, IDLE_PH]},
        "7": {"t": [0], "p": [12.0], "ph": [IDLE_PH]},
    }
    ps = phase_stats(readings["4"])
    assert ps["idle_w"] == 10.5 and ps["peak_w"] == 10.5 and ps["bench_samples"] == 0
    enrich(runs, readings)
    assert runs[0]["energy_wh"] is None
    assert runs[3]["idle_w"] == 10 and runs[3]["peak_w"] == 10.5
    assert runs[3]["valid"] and runs[3]["quality_reason"] == "valid idle capture"
    assert not runs[5]["valid"] and "invalidated" in runs[5]["quality_reason"]
    bad = mk(9, "h", "t", 0, 50, bench_sample_coverage=0.5, bench_end=1)
    assert not quality(bad, phase_stats(None))[0]
    nodur = {"run_id": 99, "energy_wh_integrated": None, "energy_wh_bench_start": None,
             "energy_wh_bench_end": None, "bench_start": None, "bench_end": None,
             "test": "t", "dropped_packets": 0, "invalid_reason": None,
             "bench_score": 1, "bench_sample_coverage": None}
    enrich([nodur], {})
    assert nodur["load_w"] is None and not nodur["valid"]
    score_runs(runs)
    byid = {r["run_id"]: r for r in runs}
    # percentile ranks are mid-rank on ties, so the cohort best lands just under 100
    assert byid[1]["score"] == 83.3 and byid[2]["score"] == 83.3 and byid[3]["score"] == 50.0
    # idle bucket ignores performance entirely: lower idle power wins
    assert byid[4]["score"] == 83.3 and byid[5]["score"] == 16.7
    # idle runs score even without a scale mapping (power side only)
    assert byid[7]["score"] == 50.0
    assert byid[6]["score"] is None  # invalid runs never scored
    # --- scale mapping ---
    assert map_scale("epp=performance") == 0
    assert map_scale("baseline") == 0
    assert map_scale("epp=balance_performance") == 25
    assert map_scale("cpu_governor=powersave") == 75
    assert map_scale("pcie_aspm=powersave") == 75
    assert map_scale("pcie_aspm=powersupersave") == 100
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
    # Readings go to per-run files fetched on demand; bundling them would make
    # data.json ~130MB. Stride-downsample long traces (charts don't need more).
    readings_dir = out.parent / "readings"
    readings_dir.mkdir(exist_ok=True)
    for old in readings_dir.glob("*.json"):
        old.unlink()
    total = 0
    for run_id, rd in data.pop("readings").items():
        stride = max(1, len(rd["t"]) // 2000)
        if stride > 1:
            rd = {k: v[::stride] for k, v in rd.items()}
        total += len(rd["t"])
        (readings_dir / f"{run_id}.json").write_text(json.dumps(rd))
    raw = data.pop("_raw")
    raw_path = out.parent / "raw.json"
    raw_path.write_text(json.dumps(raw, default=str))
    out.write_text(json.dumps(data, default=str))  # TIMESTAMP cols -> str
    print(f"{len(data['runs'])} runs, {total} readings, {sum(len(t['rows']) for t in raw.values())} preview rows "
          f"-> {out} + {readings_dir}/ + {raw_path.name}")


if __name__ == "__main__":
    main()
