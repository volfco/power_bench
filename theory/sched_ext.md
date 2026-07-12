# sched_ext (SCX) BPF schedulers

A multi-variant sweep, not a single on/off optimization — each SCX scheduler (and its
power mode) is screened against the baseline (in-kernel EEVDF) using the standard
harness. Follows the methodology in `../plan.md`.

## Hypothesis

Replacing the in-kernel default scheduler (EEVDF) with a sched_ext BPF scheduler
changes how tasks are placed, migrated, and woken — which moves **energy-to-complete**
and **load power**, and for tick-reducing schedulers, **idle power**. Specifically:

- **Power-aware modes** (`scx_lavd --powersave`, `scx_bpfland -m powersave`) consolidate
  work onto fewer / more-efficient cores and bias toward lower frequencies → lower
  energy-to-complete at a small throughput cost.
- **Locality schedulers** (`scx_rusty`) reduce cross-domain migrations on multi-CCX /
  NUMA chips → better cache behaviour → less work → better perf-per-joule.
- **`scx_tickless`** cuts timer interrupts → measurable idle *and* load power savings on
  high-core-count systems.
- On a small, homogeneous desktop CPU, several of these deltas may fall within run
  noise — that is itself a finding.

## Mechanism / background

- sched_ext (merged in Linux 6.12) lets a BPF program implement the CPU scheduler.
  The userspace schedulers live in <https://github.com/sched-ext/scx> (packaged as
  `scx` on Debian/Ubuntu, `scx-scheds` on Fedora/RHEL). Running the binary loads it;
  SIGINT/SIGTERM unloads it and the kernel reverts to EEVDF. A crashing BPF scheduler
  is auto-ejected by the kernel watchdog (also reverting to EEVDF).
- Power levers a scheduler controls: **task consolidation** (idle cores reach deeper
  C-states), **hybrid P/E-core awareness**, **migration/locality** (cache misses cost
  energy), and **tick reduction**.
- **Hardware dependence:** the largest power wins are on hybrid (Intel P/E, 12th gen+)
  and multi-CCX / NUMA AMD parts. `run_benchmark.py` records `cpu_model` per run —
  correlate every verdict with the CPU it was measured on.

## Candidate matrix

Each variant is one OFAT experiment vs. the `baseline` row. Flag names differ across
SCX releases — confirm with `<scheduler> --help` on the target before the sweep.

| Variant label (`--optimization`)   | scx_scheduler  | scx_flags        | Target  | Hypothesis                                         |
|-------------------------------------|----------------|------------------|---------|----------------------------------------------------|
| `baseline`                          | `none`         | —                | —       | reference (in-kernel EEVDF)                        |
| `sched_ext=scx_lavd:powersave`      | `scx_lavd`     | `--powersave`    | L (+I)  | consolidate + prefer efficiency → lowest energy    |
| `sched_ext=scx_lavd:performance`    | `scx_lavd`     | `--performance`  | L       | throughput upper bound for the perf floor          |
| `sched_ext=scx_bpfland:powersave`  | `scx_bpfland`  | `-m powersave`   | L (+I)  | prefer E-cores / lowest-freq CPUs                  |
| `sched_ext=scx_rusty`               | `scx_rusty`    | —                | L       | multi-domain locality → better perf/joule          |
| `sched_ext=scx_tickless`            | `scx_tickless` | —                | I+L     | excluded on this rig; v1.1.1 self-ejects at attach |
| `sched_ext=scx_flash`               | `scx_flash`    | —                | L       | EDF latency scheduler; perf/joule sanity check     |

## Implementation

- Playbook: `ansible/optimizations/sched_ext.yml` (vars `scx_scheduler`, `scx_flags`).
  Runs the chosen scheduler from a self-contained `scx-power.service`.
- **Apply:** `ansible-playbook -i ansible/hosts ansible/optimizations/sched_ext.yml -e scx_scheduler=scx_lavd -e "scx_flags=--powersave"`
- **Revert:** same playbook with `-e scx_scheduler=none`, or `reset_to_baseline.yml`
  (which stops `scx-power.service` and `scx.service`).
- **Verify it took effect:** `cat /sys/kernel/sched_ext/state` → `enabled`, and
  `cat /sys/kernel/sched_ext/root/ops` → the scheduler's ops name (e.g. `lavd`,
  `bpfland`, `rusty`). No reboot required to apply or revert.
- **Prerequisite:** kernel ≥ 6.12 with `CONFIG_SCHED_CLASS_EXT`. The playbook fails
  loudly if `/sys/kernel/sched_ext` is absent.

### Target installation / version audit

Ubuntu 26.04 on this rig has no `scx` apt package, so the source installation is
explicit and reproducible rather than a best-effort package attempt:

```bash
ansible-playbook -i ansible/hosts ansible/install_scx.yml
```

The installer pins upstream `sched-ext/scx` v1.1.1 (`0eedd05bc233129fd3c884d7045edeb2c2a474a7`),
builds the five scheduler binaries in this matrix (Tickless is installed only for
manual compatibility auditing and remains excluded from the unattended catalog), and exposes the audited executables through
`/usr/local/bin`. It requires the upstream Ubuntu build dependencies, including Rust,
Clang, libbpf, libelf, pahole, protobuf, and libseccomp. The 2026-07-10 target audit
found Rust 1.93, Clang 21, libbpf 1.6, `bpftool` 7.7, kernel BTF, and all required
`CONFIG_*` options; each exceeds upstream's stated minimums.

Version-specific flags verified on the installed binaries:

- `scx_lavd --powersave` and `scx_lavd --performance`
- `scx_bpfland -m powersave`
- `scx_rusty` with its defaults
- `scx_tickless` with its defaults
- `scx_flash` with its defaults

`/sys/kernel/sched_ext/root/ops` identifies the loaded ops implementation, not the
executable verbatim. For example, v1.1.1 LAVD reports
`lavd_1.1.1_x86_64_unknown_linux_gnu`; do not reject this as a failed apply merely
because it lacks the `scx_` executable prefix.

## Run matrix & driver

The SCX schedulers are part of the catalog in the top-level `run_suite.py` driver
(variants `sched_ext=scx_lavd:powersave`, `:performance`, `scx_bpfland:powersave`,
`scx_rusty`, and `scx_flash`). The driver runs the OFAT loop — write varfile → apply → benchmark →
reboot — for every (variant, test, repeat), with `baseline` as the reference.
`scx_tickless` is deliberately excluded on this rig; see the screening result below.

```bash
# just the sched_ext variants (baseline is always included for comparison):
python run_suite.py 192.168.1.58 --user metrolla \
    --only sched_ext \
    --tests local/power-bench-build-kernel-defconfig-1.0.0 \
    --repeats 4 --initial-reboot \
    --mac 45:AF:4E:55:56:06 --checksum-policy warn --cool-to 55

python run_suite.py 192.168.1.58 --list    # show the full matrix, including the SCX rows
```

To run one SCX variant by hand on a freshly booted host:

```bash
ansible-playbook -i ansible/hosts ansible/apply_optimizations.yml \
    -e scx_scheduler=scx_lavd -e "scx_flags=--powersave"
python run_benchmark.py 192.168.1.58 local/power-bench-build-kernel-defconfig-1.0.0 \
    --optimization sched_ext=scx_lavd:powersave --repeat 1 --reboot --user metrolla \
    --mac 45:AF:4E:55:56:06 --checksum-policy warn --cool-to 55
```

Edit the SCX rows in `run_suite.py`'s `EXPERIMENTS` to add/remove schedulers or flags.

## Measurement (mean ± stdev over N repeats, all repeats counted)

One table per test; one row per variant. Fill from the phase-aware SQL in `plan.md`.

### Test: `local/power-bench-build-kernel-defconfig-1.0.0` (lower-is-better, seconds)

Judge on energy-to-complete while keeping build time within the 95% performance floor.

| Variant                          | Idle W | Energy Wh | Build s | Avg load W | Coverage |
|----------------------------------|--------|-----------|---------|------------|----------|
| baseline (N=5)                   | —      | 3.301     | 135.590 | 79.004     | 98.4%    |
| scx_lavd:powersave (N=1)         | —      | 3.324     | 148.624 | 70.936     | 98.4%    |
| scx_lavd:performance (N=1)       | —      | 3.297     | 148.845 | 72.567     | 98.2%    |
| scx_bpfland:powersave (N=1)      | —      | 3.311     | 155.889 | 67.035     | 98.3%    |
| scx_rusty (N=1)                  | —      | 3.470     | 165.135 | 67.219     | 98.2%    |
| scx_flash (N=1)                  | 12.167 | 3.386     | 137.912 | 79.544     | 98.5%    |
| scx_tickless                     | —      | —         | —       | —          | attach failed |

### Optional future test: `pts/llama-cpp` (higher-is-better, tok/s)

Only add this after confirming the target has enough disk space for the profile's
current download/environment requirements.

## Analysis guidance

- **Idle power** should be ≈ unchanged across schedulers — *except* `scx_tickless`. A
  large idle delta from a non-tickless scheduler is suspicious (background bookkeeping
  or a misload); check it.
- **The key caveat:** PTS throughput benchmarks do **not** capture interactivity /
  tail latency — which is exactly where `scx_lavd` and `scx_bpfland` are designed to
  win. So the realistic positive result here is *"no throughput regression + lower
  energy-to-complete"*, not a big score jump. If the production workload is interactive
  (desktop, gaming, latency-sensitive services), measure that separately; these batch
  numbers under-represent the benefit.
- **BPF overhead:** a scheduler can add its own CPU cost. Watch avg load power relative
  to score — higher power for equal work is a regression even if the score holds.
- Compare every Δ to its stdev (Δ > ~2σ) before calling an effect real.

## Verdict (per variant)

- `scx_lavd:powersave`   — [ ] Keep  [x] Revert  [ ] Refine
- `scx_bpfland:powersave`— [ ] Keep  [x] Revert  [ ] Refine
- `scx_rusty`            — [ ] Keep  [x] Revert  [ ] Refine
- `scx_flash`            — [ ] Keep  [x] Revert  [ ] Refine (energy regression)
- `scx_tickless`         — [ ] Keep  [x] Revert  [ ] Refine (incompatible)
- `scx_lavd:performance` — [ ] Keep  [x] Revert  [ ] Refine (perf-ceiling reference)

### Current screen status (2026-07-10 PDT)

The initial `scx_lavd:powersave` attempt, run `#80`, is invalid because the Atorch meter
was unavailable; it has no readings or benchmark result. After restarting and validating
the meter under `--checksum-policy warn`, runs `#81`-`#84` completed with coverage
>=0.982 and zero dropped packets. The refreshed 95% floor is `142.726 s`.

- LAVD powersave: `148.624 s` (+9.61%), `3.324 Wh` (+0.69%).
- LAVD performance: `148.845 s` (+9.78%), `3.297 Wh` (-0.13%).
- BPFLand powersave: `155.889 s` (+14.97%), `3.311 Wh` (+0.29%).
- Rusty: `165.135 s` (+21.79%), `3.470 Wh` (+5.12%).

The four initial runnable schedulers all fail the performance floor; the small
LAVD-performance energy difference is one-run noise and does not offset its 9.78%
regression. Revert them and do not repeat them for this compile workload.

The previously documented but unbuilt `scx_flash` candidate was then source-built from
the same pin and screened in valid run `#85`. It attached as
`flash_1.1.1_x86_64_unknown_linux_gnu`, scored `137.912 s` (+1.71%, retaining 98.32%
of baseline performance), and used `3.386 Wh` (+2.57%), with 98.47% sample coverage
and zero drops. It passes the 95% performance floor but fails the primary fixed-work
energy criterion, so reject it after this one screen. Its 30-second gated idle window
was an unusually high 12.167 W; Flash was a load-only candidate and this short window
is not a standalone idle result, but it provides no reason to promote an idle test.
Log: `logs/sched_ext_flash_screen_20260710.log`.

Tickless had no valid run. On `7.0.0-27-generic`, SCX v1.1.1 self-ejected at attach
with `scx_bpf_error: starting timer on cpu8, which is not a scheduling CPU`, after also
warning that `nohz_full` is disabled. It is excluded from the automated catalog until a
deliberate upstream-version and flag re-audit establishes compatibility.

Winner (if any) carries into Phase B stacking. Note interactions: a scheduler's gain
may shrink once the CPU governor / turbo settings from earlier in the priority order
are also applied.

## Caveats / gotchas

- Kernel ≥ 6.12 with `CONFIG_SCHED_CLASS_EXT` required; the playbook aborts otherwise.
- SCX flag names change between releases — verify with `--help` on the target.
- Never leave a scheduler loaded across a `baseline` run; always `reset_to_baseline.yml`
  first (it stops SCX).
- If a run's `bench_sample_coverage` is below 90%, `dropped_packets` is high, or the
  scheduler self-ejected mid-run (check `dmesg` / `cat /sys/kernel/sched_ext/state`),
  discard and re-run. On the current meter, checksum failures are expected under
  `--checksum-policy warn`; use coverage and dropped packet count to detect real gaps.
