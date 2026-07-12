# Power Optimization Plan

## Goal

Reduce the wall-plug power of an Ansible-managed Linux system — both **idle** and
**under load** — while keeping (ideally improving) Phoronix benchmark performance.
The single number we optimize is **energy efficiency**: how much useful work the
machine does per joule consumed.

### Objective function (the decision rule)

"Lower power" and "higher performance" trade off, so we need one rule that resolves
conflicts up front:

> **Maximize performance-per-joule, subject to a performance floor.**
> Keep an optimization only if benchmark performance stays **≥ 95 % of baseline**
> *and* it improves either idle power or energy-to-complete. If performance drops
> below the floor, reject it regardless of power savings.

Idle power and load efficiency are reported and judged **separately** — a C-state
tweak that helps idle but does nothing under load is still a win for the idle column.

## What we measure (and why)

| Metric                      | Source                                                 | Why this and not the obvious thing                                              |
|-----------------------------|--------------------------------------------------------|---------------------------------------------------------------------------------|
| **Idle power (W)**          | meter true power, averaged over a *stable* idle window | The before/after we care about for an idle machine                              |
| **Energy-to-complete (Wh)** | trapezoidal integration of the 1 Hz true-power samples over the bench window | The real "cost" of the workload. The meter's energy counter ticks in 10 Wh steps — too coarse at run length; its delta is stored only as a cross-check |
| **Benchmark result**        | PTS result XML (score + unit + higher/lower-is-better) | Required for the performance floor and for perf/joule. **Must be captured**     |
| **Performance-per-joule**   | score ÷ energy (HIB) or work ÷ energy (LIB)            | The thing we actually optimize. See note below                                  |
| **Avg/peak load power (W)** | meter true power over the bench window                 | Secondary; useful for thermal/PSU headroom, not for the verdict                 |

**Use true power, not V×I.** This is an **AC mains meter** (it reports line
frequency and power factor). Real power ≠ V × I when power factor < 1 (every PC PSU).
We must record and use the meter's reported `watt` field and `energy_wh`, *not* a
computed `voltage × current` (which is apparent power / VA and overstates watts).
*(If the rig is ever switched to a DC/USB rail where the meter has no true-power
field, fall back to V×I and note it in the run metadata.)*

**The meter's energy counter is too coarse for per-run energy.** The AC report's
energy field is a uint32 in **0.01 kWh units — 10 Wh per tick** (per the Atorch
protocol doc; the first live runs confirmed the counter often remains unchanged for
short benchmarks). A 15-minute 150 W benchmark is under 4 ticks, so differencing the counter
carries up to ±10 Wh (~25 %) quantization error — worse than run-to-run noise and
larger than the 20 % efficiency threshold we're judging. Per-run energy therefore
comes from **integrating the 1 Hz `watt` samples** (trapezoid, `Σ P·dt / 3600`,
error ≪ 1 % at these durations), stored as `runs.energy_wh_integrated`. The counter
delta is recorded alongside purely as a long-window sanity cross-check.

**Performance-per-joule depends on benchmark direction.** Phoronix tests are either
higher-is-better (e.g. `llama-cpp` tokens/s) or lower-is-better (e.g.
the defconfig-only kernel build suite's seconds). Store the direction with each result and compute:
- **HIB:** `score / energy_wh` (work per Wh — higher is better)
- **LIB:** for a fixed amount of work, the figure of merit is just **energy-to-complete**
  (Wh) at equal-or-better wall-time — lower is better.

## Tests to run

| Test (PTS profile/suite)                                  | Direction                | Stresses              | Notes                                                                    |
|-----------------------------------------------------------|--------------------------|-----------------------|--------------------------------------------------------------------------|
| `local/power-bench-build-kernel-defconfig-1.0.0`          | lower-is-better (s)      | CPU bursty + I/O      | Local suite pins `pts/build-linux-kernel` to `defconfig` only            |
| `pts/llama-cpp`                                           | higher-is-better (tok/s) | CPU/mem, sustained    | Optional; can require >100 GB download/environment space in current PTS  |
| `pts/disk` (suite)                                        | mixed                    | I/O                   | Confirm exact profile id with `phoronix-test-suite list-available-tests` |
| `pts/memory` (suite)                                      | mixed                    | RAM bandwidth/latency | As above                                                                 |

Confirm each identifier exactly before runs — suite names and profile names differ.
The setup playbook owns the local `power-bench-build-kernel-defconfig-1.0.0` suite;
run the raw `pts/build-linux-kernel` profile only for debugging, because it can select
both `defconfig` and `allmodconfig`.

## Current Rig Status

These live facts are current as of 2026-07-09:

- Target: `192.168.1.58`, SSH user `metrolla`; Ansible inventory alias is `target`,
  but `run_benchmark.py` should use the IP address.
- Meter BLE MAC: `45:AF:4E:55:56:06`.
- Meter checksum policy for this rig: use `--checksum-policy warn`. The AC packets
  parse sensibly, but the trailer byte does not match the documented checksum; strict
  mode drops all useful readings.
- PTS setup: `/usr/local/bin/phoronix-test-suite` v10.8.4, non-interactive config in
  `/home/metrolla/.phoronix-test-suite/user-config.xml`, auto-loaded PTS modules
  disabled, `pts/build-linux-kernel` preinstalled, and the local defconfig-only suite
  installed by `ansible/setup_phoronix.yml`.
- `pts/llama-cpp` is not part of the default run list on this target because current
  PTS metadata requires over 100 GB of download/environment space.
- Valid baseline DB: `benchmarks/power_meter.duckdb`.
  - Run #1: long idle baseline, `4.328 W` over 590 stable idle samples.
  - Run #4: defconfig kernel build baseline, `135.15 Seconds`, `3.281 Wh`,
    `78.093 W` average load, `96.5 W` peak, `99%` sample coverage.
  - Runs #2 and #3 are invalid early setup attempts and have `bench_sample_coverage=0`.
  - Raw PTS XML for run #4 is archived at `benchmarks/pts_results/power_bench_run4.xml`.

**Match tests to the knob's target column.** Pure idle-targeted knobs (C-states,
ASPM, USB autosuspend, disk, services, NIC) are screened with a dedicated
`--idle-only` measurement plus **one** quick load test
(`local/power-bench-build-kernel-defconfig-1.0.0`) for the performance floor.
Load- and mixed-target knobs run the full test list; their (stability-gated) idle
windows are enough for idle screening. `baseline` runs both the full test list and
the idle-only reference. This roughly halves the sweep.

## Experimental design

Two phases, because OFAT and cumulative answer different questions:

1. **Phase A — screen (one factor at a time).** Start from baseline, apply exactly
   **one** optimization, measure, then **reset to baseline** before the next. Clean
   attribution: each delta is caused by that one change.
2. **Phase B — stack winners.** Take every optimization that passed in Phase A and
   apply them cumulatively in priority order, re-measuring after each addition. This
   catches interactions (e.g. governor × turbo) that OFAT can't see. If a stacked
   addition regresses below the floor, drop it.
3. **Phase C — confirm.** Run the final stacked config 5× and report mean ± stdev
   against baseline for every metric and every test.

Controls applied to **every** run:
- **Fresh boot per test** — each iteration applies a varfile of *only its non-default
  knobs* to a freshly booted host, runs, then reboots. Every knob is applied
  non-persistently, so the reboot IS the reset; no run measures on leftover state.
  (See *Per-iteration apply* below.)
- **N ≥ 3 repeats** per (config, test); report mean ± sample stdev. Phase C uses N = 5.
- **Randomize run order** across configs to spread out drift (don't always measure
  baseline first thing in the morning and the tuned config when the room is warm).
- **Thermal gate (replaces the warm-up discard):** before the bench phase starts,
  poll the host's CPU temperature over SSH until it is at/below the `--cool-to`
  threshold (or timeout, then proceed with a warning) and record the start
  temperature in `runs.bench_start_temp_c`. Every run starts freshly booted, so
  there is no cache/compile warm-up to discard — **all repeats are counted**
  (`repeat_idx` runs 1..N).
- **Idle stability gate (enforced in `run_benchmark.py`):** settling samples are
  tagged `phase='settle'`; the window flips to `phase='idle'` only once the rolling
  power stdev drops below a threshold (with a timeout) — a machine still settling
  post-boot is never averaged into idle. Idle-targeted variants additionally get a
  dedicated `--idle-only` run (long stable idle, no benchmark) that produces the
  idle-power number for the verdict.
- **Hold the environment constant:** ambient temperature, same PSU/cabling, same
  kernel image except for the param under test, networking quiesced.
- **Fail → reboot:** any failure path (apply error, benchmark abort, meter loss)
  reboots the host before the next iteration, so a failed run can never leak its
  knobs into the next one.

### Per-iteration apply: one varfile, freshly booted each time

The unit of work is a **varfile**. For each iteration the driver (`run_suite.py`):

1. **Generates `ansible/vars/iter_<NNNN>_<label>.yml`** containing *only the knobs whose
   value differs from `ansible/vars/defaults.yml`* (the fresh-boot baseline). A baseline
   iteration's varfile is empty.
2. **Applies it:** `ansible-playbook -i ansible/hosts ansible/apply_optimizations.yml -e @<varfile>`.
   The master play sets each knob only when the varfile defines it — anything omitted
   stays at the fresh-boot default. All knobs are applied at **runtime (non-persistent)**.
3. **Verifies the config took:** the apply play ends by reading back every knob it
   set (sysfs values; `/proc/cmdline` for kernel params) and fails loudly on any
   mismatch — many knobs are best-effort writes, and a silent no-op would mislabel
   the run as its variant while actually measuring baseline. `run_benchmark.py`
   additionally stores a read-back snapshot (`applied_config` JSON) and the varfile
   hash (`config_hash`) in the run row, so a mislabeled run is detectable in SQL.
4. **Runs the benchmark:** `run_benchmark.py … --optimization <label> --reboot`.
5. **Reboots** at the end → the next iteration starts on a clean baseline. If any
   step fails, the driver reboots before continuing, and the sweep finishes with one
   reconcile-to-defaults apply so a final `kernel_params` variant can't leave GRUB
   dirty.

Why this works: a fresh boot returns every runtime knob (governor, turbo, C-states, EPP,
ASPM, I/O scheduler, USB, disk link PM, GPU, NIC, stopped services, loaded SCX scheduler)
to its default, so the reboot replaces an explicit reset. The **one** persistent setting
is kernel boot params (GRUB); `apply_optimizations.yml` reconciles the managed cmdline
fragment to the varfile's `kernel_params` (default `[]`) on every run — clearing a prior
iteration's params — and reboots mid-apply if it changed, so the benchmark always runs on
the intended cmdline. To keep this guarantee, services are *stopped* (not disabled) and the
SCX unit is *started but not enabled*, so a reboot restores both. `reset_to_baseline.yml`
remains for manual, no-reboot cleanup.

## Data model

Use a **single DuckDB file** with run metadata, not one opaque file per run — then
cross-run comparison is a query, not file juggling. **Implemented in `database.py`**;
the schema below is what the code creates (`run_id` auto-increments via a sequence and
`started_at` defaults to now). An older `readings` table is auto-migrated with
`ALTER TABLE … ADD COLUMN IF NOT EXISTS run_id / phase`.

```sql
-- One row per benchmark invocation
CREATE TABLE runs (
    run_id          INTEGER PRIMARY KEY,   -- DEFAULT nextval('runs_seq')
    started_at      TIMESTAMP,             -- DEFAULT current_timestamp
    host            VARCHAR,
    test            VARCHAR,     -- PTS profile, or 'idle' for --idle-only runs
    optimization    VARCHAR,     -- label, e.g. 'baseline', 'cpu_governor=powersave'
    config_hash     VARCHAR,     -- hash of the varfile's non-default knobs (always set)
    repeat_idx      INTEGER,     -- 1..N, all counted (no warm-up discard)
    result_name     VARCHAR,     -- PTS result set name, 'power_bench_run<id>'
    -- environment snapshot (the "Baseline Configuration" list, structured)
    kernel          VARCHAR,
    cpu_model       VARCHAR,
    governor        VARCHAR,
    turbo           VARCHAR,     -- intel_pstate/no_turbo or cpufreq/boost (AMD)
    ambient_c       DOUBLE,      -- auto-filled from the meter temperature if not given
    applied_config  VARCHAR,     -- JSON read-back of every knob after apply
    bench_start_temp_c DOUBLE,   -- CPU temp when the bench phase started (thermal gate)
    -- phase boundaries (epoch seconds) — exact, settle-independent
    idle_start      DOUBLE,
    bench_start     DOUBLE,
    bench_end       DOUBLE,
    -- energy: integrated from 1 Hz samples (primary) + counter delta (cross-check)
    energy_wh_integrated  DOUBLE,   -- trapezoid over bench-phase power samples
    energy_wh_bench_start DOUBLE,   -- meter counter snapshots (10 Wh resolution)
    energy_wh_bench_end   DOUBLE,
    -- benchmark result, retrieved from PTS (primary; every entry lands in run_results)
    bench_score     DOUBLE,
    bench_unit      VARCHAR,
    higher_is_better BOOLEAN,
    -- sampling quality
    dropped_packets INTEGER,
    checksum_failures INTEGER,
    bench_sample_coverage DOUBLE    -- actual/expected bench samples; < 0.9 → invalid run
);

-- Every PTS <Result> entry (llama-cpp alone emits several; suites emit many).
-- runs.bench_score keeps the first/primary one for convenience.
CREATE TABLE run_results (
    run_id           INTEGER,
    title            VARCHAR,
    scale            VARCHAR,
    higher_is_better BOOLEAN,
    value            DOUBLE
);

-- readings gain a run_id FK and an explicit phase tag
ALTER TABLE readings ADD COLUMN run_id INTEGER;
ALTER TABLE readings ADD COLUMN phase  VARCHAR;  -- 'settle' | 'idle' | 'bench' | 'cooldown'
```

With phases tagged and energy integrated, the metrics become exact and
settle-independent:

```sql
-- Idle power (only the stable, gated idle window — 'settle' samples are excluded)
SELECT AVG(power_w) FROM readings WHERE run_id = ? AND phase = 'idle';

-- Energy-to-complete the workload (integrated; counter delta is the cross-check)
SELECT energy_wh_integrated,
       energy_wh_bench_end - energy_wh_bench_start AS counter_delta_wh
FROM runs WHERE run_id = ?;

-- Avg / peak load power
SELECT AVG(power_w), MAX(power_w) FROM readings WHERE run_id = ? AND phase = 'bench';

-- Performance-per-joule (HIB) or energy-to-complete (LIB), per optimization
SELECT optimization,
       AVG(bench_score)          AS score,
       AVG(energy_wh_integrated) AS energy_wh,
       AVG(bench_score / NULLIF(energy_wh_integrated, 0)) AS score_per_wh
FROM runs
WHERE test = ? AND COALESCE(bench_sample_coverage, 1) >= 0.9
GROUP BY optimization;
```

## Pipeline (built)

The measurement pipeline is implemented; the offline-testable parts (AC power math,
PTS XML parsing, DB round-trip + phase queries) are verified. Each capability the plan
relies on, and where it lives:

| Capability                                                                                                                                                    | Where                                                                   |
|---------------------------------------------------------------------------------------------------------------------------------------------------------------|-------------------------------------------------------------------------|
| Phoronix result captured — deterministic `TEST_RESULTS_NAME`, `composite.xml` pulled over SSH, score + unit + direction (HIB/LIB) parsed and stored in `runs` | `pts_results.parse_composite_xml`, `run_benchmark.fetch_pts_result`     |
| **True AC power** (`watt` field, not V×I → no power-factor error); per-run energy **integrated from samples**, counter delta (10 Wh/tick) kept as cross-check | `atorch_protocol.parse_report`, `run_benchmark.run_async`               |
| Phases tagged on every reading + run metadata (host/kernel/governor/turbo snapshot)                                                                           | `database` (`runs`, `readings.phase`), `run_benchmark.gather_host_info` |
| Event-loop stall fixed — remote stdout drained in an executor so power sampling never pauses on long tests                                                    | `run_benchmark.stream_remote_output`                                    |
| Fail-fast — aborts *before* launching the benchmark if no valid meter reading arrives within 15 s                                                             | `run_benchmark.run_async`                                               |
| Checksum handled — strict mode drops corrupt packets; warn mode parses/counts them separately for meters with nonstandard trailer bytes                       | `run_benchmark.power_logger`                                            |
| Headless PTS — batch mode configured + `default-run` with `FORCE_TIMES_TO_RUN=1` / `PTS_SILENT_MODE=1` so profile defaults are selected without prompts                                                                       | `ansible/setup_phoronix.yml`, `run_benchmark.launch_benchmark`          |
| Phase-aware summary (idle / load / energy-to-complete / perf-per-Wh) replaces the old blended average                                                         | `run_benchmark.print_summary`                                           |
| Runtime reset playbook (governor, turbo, ASPM, I/O sched, USB, C-states)                                                                                      | `ansible/reset_to_baseline.yml`                                         |
| Post-apply verification — sysfs / `/proc/cmdline` read-back asserted by the play; `applied_config` snapshot + `config_hash` stored on every run               | `ansible/apply_optimizations.yml`, `run_benchmark.gather_host_info`     |
| Idle stability gate (`settle`→`idle` phase flip) + `--idle-only` mode; pre-bench thermal gate (`--cool-to`), start temp recorded                              | `run_benchmark.run_async`                                               |
| Sample-coverage validity check (catches silent BLE gaps / stale energy counter) — `bench_sample_coverage` stored, < 0.9 flagged                               | `run_benchmark`, `database`                                             |
| **All** PTS result entries stored (`run_results` table); raw `composite.xml` archived locally per run                                                         | `database`, `run_benchmark.fetch_pts_result`                            |
| Failure-path reboots (benchmark abort, apply error) + end-of-sweep reconcile-to-defaults                                                                      | `run_benchmark.run`, `run_suite`                                        |
| Test profiles pre-installed per test (`batch-install`) so no download/compile lands inside a measured run; measured execution uses `default-run` and a defconfig-only local suite to avoid profile-option prompts                     | `ansible/setup_phoronix.yml`                                            |
| PTS sanitized result-directory names handled (`power_bench_run4` may be saved as `powerbenchrun4`)                                                             | `run_benchmark.pts_result_name_candidates`                              |

**Live-rig confirmations:**

- If a host already has a populated `~/.phoronix-test-suite/user-config.xml`, the
  setup task's `force: false` won't overwrite it — run `phoronix-test-suite
  batch-setup` once, or delete the file and re-run `setup_phoronix.yml`.
- PTS sanitizes result directory names. For example, the run result name
  `power_bench_run4` was saved under
  `~/.phoronix-test-suite/test-results/powerbenchrun4/`; the fetcher now tries both
  the requested and sanitized names.
- Boot-parameter changes are reconciled by `apply_optimizations.yml` (it rewrites the
  managed GRUB fragment to the varfile each run, reboots if needed, and asserts
  `/proc/cmdline` afterwards); BIOS-level reverts still need manual intervention.
- The AC energy counter did not move during the valid 135 s baseline build, as
  expected from the 10 Wh tick size. Integrated sample energy remains primary.

**Apply mechanism (built):** per-iteration varfiles + `ansible/apply_optimizations.yml`
+ the `run_suite.py` driver (see *Per-iteration apply* below). The standalone
`ansible/optimizations/*.yml` playbooks remain as manual single-knob tools, and all
`theory/*.md` docs are scaffolded.

## Directory structure

```
# measurement pipeline (built)
CODEX_CONTINUE.md                # handoff brief for the next Codex testing/tuning session
run_benchmark.py                 # one test run: phases, power logging, PTS result fetch, end reboot
run_suite.py                     # sweep driver: per iteration → varfile → apply → benchmark → reboot
atorch_protocol.py               # Atorch BLE packet parser (true power for AC)
meter_ble.py                     # BLE connection handler
database.py                      # DuckDB storage: runs + readings tables
pts_results.py                   # parse PTS composite.xml into score/unit/direction

ansible/
  hosts                          # inventory
  setup_phoronix.yml             # base PTS setup + non-interactive batch mode
  run_core_power_bench.yml       # new-node setup + 3-repeat core matrix entry point
  apply_optimizations.yml        # master: apply a varfile's non-default knobs to the host
  reset_to_baseline.yml          # manual no-reboot cleanup (the sweep resets via reboot)
  vars/
    defaults.yml                 # fresh-boot defaults (reference for the generator)
    iter_*.yml                   # per-iteration varfiles (generated by run_suite.py)
  optimizations/                 # standalone single-knob playbooks (manual use) + baseline.yml
    baseline.yml  cpu_governor.yml  turbo_boost.yml  c_states.yml  p_states.yml
    pcie_aspm.yml  io_scheduler.yml  kernel_params.yml  usb_autosuspend.yml
    disk_power.yml  gpu_power.yml  services.yml  network_power.yml  sched_ext.yml

theory/                          # one doc per optimization (all scaffolded)
  baseline.md  cpu_governor.md  turbo_boost.md  c_states.md  p_states.md  pcie_aspm.md
  io_scheduler.md  kernel_params.md  usb_autosuspend.md  disk_power.md  gpu_power.md
  services.md  network_power.md  sched_ext.md

benchmarks/
  power_meter.duckdb             # single DB, runs + readings (auto-created on first run)
  pts_results/*.xml              # archived PTS composite.xml files
  results.csv                    # consolidated, exported from the DB
```

> Apply optimizations **explicitly by name and in priority order** — never
> `optimizations/*.yml`, whose alphabetical glob order does not match the priority
> table and silently stacks changes.

## Theory document template

```markdown
# [Optimization Name]

## Hypothesis
What we expect to change and the mechanism (why it should help idle / load / both).

## Implementation
Exact Ansible task, sysctl/boot values, and whether a reboot is required to apply
*and* to revert.

## Measurement (mean ± stdev over N repeats, all repeats counted)
| Metric              | Baseline      | This change   | Δ        |
|---------------------|---------------|---------------|----------|
| Idle power (W)      |               |               |          |
| Energy-to-complete (Wh) |           |               |          |
| Benchmark result    |               |               |          |
| Perf-per-joule      |               |               |          |
| Avg load power (W)  |               |               |          |

Test(s): ...   N repeats: ...   Run order: randomized   Ambient: ... °C

## Analysis
Did the hypothesis hold? Is the change above measurement noise (compare Δ to stdev)?
Any interaction observed in Phase B stacking?

## Verdict
- [ ] Keep   - [ ] Revert   - [ ] Refine (notes: ...)
Decision against the floor: performance ≥ 95 % of baseline? efficiency improved?
```

## Optimizations (priority order)

Tagged by which objective they target (I = idle, L = load) so wins land in the right
column.

| #  | Optimization                                         | Target            | Expected impact           | Risk                     | Reboot to apply/revert? |
|----|------------------------------------------------------|-------------------|---------------------------|--------------------------|-------------------------|
| 1  | Baseline capture                                     | —                 | reference                 | none                     | no                      |
| 2  | CPU governor (`powersave`/`schedutil`/`performance`) | I+L               | high                      | low (single-thread perf) | no                      |
| 3  | Turbo boost control                                  | L                 | high (turbo can ~2× draw) | med (burst perf)         | no                      |
| 4  | C-state limits                                       | I                 | medium                    | low (wake latency)       | maybe                   |
| 5  | intel_pstate / HWP tuning                            | I+L               | medium                    | low                      | maybe                   |
| 6  | PCIe ASPM                                            | I                 | medium                    | low                      | yes (boot param)        |
| 7  | I/O scheduler (`none`/`mq-deadline`/`kyber`)         | L                 | low-med                   | low                      | no                      |
| 8  | Kernel boot params                                   | I+L               | medium                    | low                      | **yes**                 |
| 9  | USB autosuspend                                      | I                 | low                       | none                     | no                      |
| 10 | Disk APM/AAM                                         | I                 | low                       | none (latency)           | no                      |
| 11 | GPU power management                                 | I/L (if GPU load) | low                       | none                     | no                      |
| 12 | Service trimming                                     | I                 | low                       | low                      | no                      |
| 13 | NIC power (EEE, WoL off)                             | I                 | low                       | none                     | no                      |
| 14 | sched_ext BPF scheduler (`scx_lavd`/`scx_bpfland`/`scx_rusty`/`scx_tickless`) | L (I for tickless) | med-high on hybrid/NUMA, noise on small homogeneous CPUs | low-med (BPF sched; kernel ≥ 6.12) | no |

Row 14 is a **multi-scheduler sweep**, not a single toggle — each SCX scheduler/mode is
screened against baseline. See `theory/sched_ext.md` for the candidate matrix, the
`ansible/optimizations/sched_ext.yml` usage, and the run driver.

## Per-optimization workflow

1. **Theory doc first.** Write `theory/<name>.md` (hypothesis + mechanism) *before*
   changing anything, and add the variant to the `EXPERIMENTS` list in `run_suite.py`
   (a `(label, {non-default knobs})` tuple).
2. **Run the sweep.** `run_suite.py` drives every (variant, test, repeat) iteration —
   generate varfile → apply → benchmark → reboot:
   ```bash
   python run_suite.py 192.168.1.58 --user metrolla \
     --tests local/power-bench-build-kernel-defconfig-1.0.0 \
     --repeats 3 --initial-reboot \
     --mac 45:AF:4E:55:56:06 --checksum-policy warn --cool-to 55
   ```
   It ships the full OFAT catalog (all 13 optimizations + their candidate values).
   `--list` prints the matrix, `--only <name>` runs a subset (baseline always included),
   `--shuffle --seed N` randomizes run order, `--dry-run` shows commands without executing.
   To run a single variant by hand (host must be freshly booted):
   ```bash
   ansible-playbook -i ansible/hosts ansible/apply_optimizations.yml -e @ansible/vars/<iter>.yml
   python run_benchmark.py 192.168.1.58 <test> \
     --optimization <label> --repeat <r> --reboot --user metrolla \
     --mac 45:AF:4E:55:56:06 --checksum-policy warn --cool-to 55
   ```
   Each `run_benchmark.py` invocation logs true AC power, walks settle → idle →
   benchmark → cooldown, integrates bench-phase energy from the samples (counter
   delta stored as cross-check), retrieves the PTS score (every result entry into
   `run_results`, raw XML archived), writes one `runs` row + phase-tagged `readings`,
   then reboots the host so the next run starts freshly booted — including on failure
   paths. The idle-stability gate and pre-bench thermal gate are enforced by
   `run_benchmark.py` itself; all repeats are counted (no warm-up discard). Pure
   idle-targeted variants run `--idle-only` plus the defconfig-only kernel build
   suite instead of the full test list.
3. **Evaluate** with the phase-aware SQL in the Data model section; compare Δ to
   stdev to confirm the effect is real.
4. **Update the theory doc** (Measurement, Analysis, Verdict).
5. **Append to `results.csv`** (export from the DB so it stays consistent).

### Repeatable new-node core suite

For a new node, use the single controller-side entry point below rather than invoking
the setup, apply, and Python drivers separately:

```bash
ansible-playbook -i inventory.yml ansible/run_core_power_bench.yml --limit node-a \
  -e power_bench_meter_mac=45:AF:4E:55:56:06
```

It runs idempotent PTS setup first, then writes all results to
`benchmarks/power_meter.duckdb`. The default core matrix is baseline,
`max_perf_pct=90`, and runtime `pcie_aspm=powersave`, with exactly three valid
repeats per measurement. That produces 15 measurement/reboot cycles: baseline idle
and build (3 each), balanced-load build (3), and ASPM idle and build (3 each). The
order is deterministically shuffled and `--skip-existing` resumes only valid rows for
the selected host, so rows from another node in the shared DuckDB cannot be reused.

The wrapper serializes nodes because the BLE meter and DuckDB writer are controller
resources, limits every nested apply to the selected inventory host, and fails if any
requested repeat lacks a valid row. It requires public-key SSH because the measurement
harness deliberately uses BatchMode. Preview the matrix without a measurement with
`-e power_bench_dry_run=true` (the idempotent PTS setup still runs); override
`power_bench_cool_to`, meter MAC, or the core-variant list only for a deliberate
node-specific change. The default `max_perf_pct=90` candidate requires Intel pstate;
for other CPU drivers, set `power_bench_core_variants` to a supported node-specific
matrix.

```csv
date,optimization,test,n,idle_power_w,idle_sd,energy_wh,energy_sd,bench_score,score_sd,perf_per_joule,verdict
2026-07-09,baseline,idle,1,4.328,,,,,,,reference
2026-07-09,baseline,local/power-bench-build-kernel-defconfig-1.0.0,1,4.350,,3.281,,135.15,,LIB_energy_wh=3.281,reference
```

## Baseline configuration (capture once, store in `runs`)

Kernel version · CPU model & core count · current governor · turbo state · available
C-states · I/O scheduler · running-services count · PCIe ASPM policy · BIOS/UEFI
power profile (if accessible) · ambient temperature · meter device type (AC/DC/USB).

## Success criteria (judged on Phase C, with variance)

A result counts only if its Δ exceeds run-to-run noise (roughly Δ > 2 × stdev; an
N = 3 stdev is itself noisy, so pool the baseline variance across its repeats for
Phase A screening and use a Welch t-test for Phase C verdicts):

- **Idle power** reduced by ≥ 15 % vs baseline (mean over N = 5 dedicated,
  stability-gated `--idle-only` runs).
- **Performance floor:** benchmark result ≥ 95 % of baseline on every test.
- **Efficiency:** performance-per-joule improved by ≥ 20 % (HIB tests), or
  energy-to-complete reduced ≥ 20 % at equal-or-better wall-time (LIB tests).

## Risks & gotchas

- **Thermal soak:** back-to-back runs heat the box; later runs throttle and look
  worse. Mitigated by the pre-bench thermal gate + randomized order; the start
  temperature is recorded with every run so soak is auditable after the fact.
- **Silent apply failures:** many knobs are best-effort sysfs writes; a knob that
  quietly fails to take produces a run labeled as its variant while measuring
  baseline. The apply play asserts read-back values (and `/proc/cmdline` for boot
  params) and the run row stores the `applied_config` snapshot — don't trust that
  the playbook ran.
- **PTS network/download** on first install adds variable, non-compute power;
  pre-install the exact test list up front (`batch-install` in
  `setup_phoronix.yml`), never inside a measured run.
- **PTS result names:** PTS may sanitize `TEST_RESULTS_NAME` when creating the
  result directory. The fetcher handles known variants, but if a score is missing,
  inspect `~/.phoronix-test-suite/test-results/` on the target and backfill from
  `composite.xml`.
- **Meter dropouts:** checksum failures are counted separately from dropped packets.
  The realistic failure is a *silent BLE gap* (stale `latest` reading, stale energy
  counter) — caught by `bench_sample_coverage` (actual/expected samples); a run
  under 90 % coverage is invalid and must be re-run, not silently averaged.
- **Two apply paths, by design.** The sweep uses `apply_optimizations.yml` (varfile-driven,
  **non-persistent** so the end-of-test reboot resets everything). The standalone
  `ansible/optimizations/*.yml` playbooks are for **manual single-knob** use and apply the
  same knobs, but two of them use **persistent** semantics — `services.yml` *disables*
  services and `sched_ext.yml`/`kernel_params.yml` *enable*/persist their change — which a
  plain reboot will **not** undo. The apply logic is therefore duplicated between the two
  paths; keep them in sync if you edit a knob.
- **Don't mix manual standalone runs into a sweep.** A persistent change left by a
  standalone playbook (a disabled service, an enabled `scx-power.service`, a GRUB param)
  survives reboots and silently contaminates the sweep baseline. Always start a sweep from
  a clean baseline — fresh OS state or `reset_to_baseline.yml` + re-enable anything a
  standalone disabled — and use `run_suite.py` (never raw `optimizations/*.yml`) for measured runs.
```
