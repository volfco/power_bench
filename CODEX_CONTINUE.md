# Codex Continuation Brief

This workspace has completed the baseline, Phase A CPU-policy sweep, targeted
idle sweep, idle diagnosis pass, and Phase C confirmation for the BLE wall-power
benchmark rig. Use this note as the current handoff state, not as a to-do list
from an older run.

## Rig Constants

- Target host: `192.168.1.58`
- SSH user: `metrolla`
- Ansible inventory alias: `target`
- BLE power meter MAC: `45:AF:4E:55:56:06`
- Required meter policy: `--checksum-policy warn`
- Default benchmark: `local/power-bench-build-kernel-defconfig-1.0.0`
- Database: `benchmarks/power_meter.duckdb`
- Target baseline state: Linux `7.0.0-27-generic`, Intel Core Ultra 5 125H, governor `powersave`, turbo on, EPP `balance_performance`, NVMe scheduler `none`

Do not use raw `pts/build-linux-kernel`; it can select both `defconfig` and `allmodconfig`. Use the local wrapper benchmark above.

## Validation State

- Python compile check passed:
  `python3 -m py_compile atorch_protocol.py database.py logger.py meter_ble.py pts_results.py run_benchmark.py run_suite.py main.py`
- Ansible syntax check passed for:
  `ansible/setup_phoronix.yml`
  `ansible/apply_optimizations.yml`
- `ansible/setup_phoronix.yml` idempotence check passed.
- Baseline varfile apply passed.
- Phase A suite completed with exit code 0.
- Targeted idle sweep completed for PCIe ASPM, USB autosuspend, SATA LPM,
  service trimming, NIC power save, and `pcie_aspm=force`.
- Final kernel-param reconcile completed successfully after the targeted idle
  sweep. Verified live `/proc/cmdline` no longer contains `pcie_aspm=force`.
- Phase C confirmation completed for `pcie_aspm=powersave` and refreshed
  baseline repeats to 5 valid idle rows and 5 valid load rows.
- Final reconcile after Phase C completed successfully. Verified live PCIe ASPM
  policy is `[default] performance powersave powersupersave` and live
  `/proc/cmdline` does not contain `pcie_aspm=force`.
- On 2026-07-10 PDT, the confirmed conservative idle profile was added at
  `ansible/profiles/conservative_idle.yml`, its apply and manual reset paths were
  live-tested, and the profile was deployed. Current live policy is
  `default performance [powersave] powersupersave`; governor remains `powersave`,
  EPP remains `balance_performance`, NVMe scheduler remains `[none]`, and the live
  cmdline still has no `pcie_aspm=force`. This profile is runtime-only and returns
  to the baseline ASPM `default` policy after reboot.
- Corrected stale manual reset defaults: `reset_to_baseline.yml` now restores this
  rig to governor `powersave` and scheduler `none`. The corrected reset was applied
  and its complete baseline read-back passed before the conservative profile was
  reapplied.
- Skipped the active-NIC experiment by user direction. Completed the next item:
  three additional `max_perf_pct=80` confirmation runs (`#66`-`#68`), all with
  coverage >=0.98 and zero dropped packets. The suite exited 0, reconciled to
  defaults, and the conservative idle profile was reapplied afterward. The N=6
  pooled load result is documented in `theory/p_states.md`, `FINAL_REPORT.md`, and
  `benchmarks/results.csv`; the optional profile is
  `ansible/profiles/relaxed_load.yml`.
- Completed the supported NVMe scheduler comparison on 2026-07-10 PDT. This rig
  exposes only `none` and `mq-deadline`; `bfq` and `kyber` are unsupported. Runs
  `#69`-`#71` tested `mq-deadline`, all with coverage >=0.98 and zero drops. Versus
  the N=5 `none` baseline it was 0.31% slower (`136.016 +/- 0.133 s`), used 0.56%
  more energy (`3.320 +/- 0.018 Wh`), and raised average load power 0.65%. None of
  the differences was significant. Revert/retain baseline `none`; do not repeat.
- sched_ext had been audited as the next load branch: kernel support exists and reports
  `disabled`, but no SCX binaries were installed and no `scx` package was available in
  the configured apt metadata. On 2026-07-10 PDT a deliberate, pinned source install
  was completed with `ansible/install_scx.yml`: upstream `sched-ext/scx` `v1.1.1`
  (`0eedd05bc233129fd3c884d7045edeb2c2a474a7`) initially built and installed
  `scx_lavd`, `scx_bpfland`, `scx_rusty`, and `scx_tickless` under
  `/usr/local/lib/power_bench/scx-1.1.1` with `/usr/local/bin` links; the same
  pinned install was later extended with `scx_flash`. Target
  toolchain/kernel prerequisites and the configured flags were audited live.
  A manual `scx_lavd --powersave` apply reached `sched_ext=enabled` and reported
  ops `lavd_1.1.1_x86_64_unknown_linux_gnu`; unload returned cleanly to `disabled`.
  The apply verifier was corrected to accept versioned ops names while using the
  service's resolved executable as the scheduler-identity check.
- The first SCX attempt, run `#80` (`scx_lavd:powersave`), is invalid: the BLE meter
  was unavailable and it has no readings or benchmark metrics. LAVD itself attached and
  cleanly unregistered during the recovery reboot; do not include `#80` in any result.
  The meter was restarted and then live-validated under the required `warn` checksum
  policy (its expected checksum-warning reports parsed normally).
- The one-repeat SCX screen completed on 2026-07-10 PDT in valid runs `#81`-`#84`, all
  with coverage >=0.982 and zero dropped packets. Against the refreshed N=5 baseline
  (`135.590 s`, `3.301 Wh`, `79.004 W`), all four schedulers in that initial screen
  missed the strict 95% performance floor (`142.726 s`) and none improved fixed-work energy:
  `scx_lavd:powersave` = `148.624 s`, `3.324 Wh` (+0.69% energy);
  `scx_lavd:performance` = `148.845 s`, `3.297 Wh` (-0.13%);
  `scx_bpfland:powersave` = `155.889 s`, `3.311 Wh` (+0.29%); and
  `scx_rusty` = `165.135 s`, `3.470 Wh` (+5.12%). Revert all four and do not
  promote any to Phase B or repeat them for this batch workload.
- `scx_tickless` v1.1.1 cannot run on this kernel/CPU topology. It self-ejected on
  every attach attempt with `scx_bpf_error: starting timer on cpu8, which is not a
  scheduling CPU` (and warned that `nohz_full` is disabled); no benchmark was started.
  It is excluded from the unattended SCX catalog until an upstream-compatible release
  and flags are deliberately audited. The conservative idle profile was reapplied after
  the screen; live state is `sched_ext=disabled`, governor `powersave`, EPP
  `balance_performance`, NVMe `none`, and PCIe ASPM `powersave`.
- The documented but previously unbuilt `scx_flash` branch was audited, built from the
  same pinned v1.1.1 source, installed, and screened in valid run `#85` on 2026-07-10
  PDT. It attached as `flash_1.1.1_x86_64_unknown_linux_gnu`, scored `137.912 s`
  (+1.71%), and used `3.386 Wh` (+2.57%), with 98.47% coverage and zero drops. It
  clears the 95% performance floor but regresses fixed-work energy, so reject it and do
  not repeat it for this compile workload. The suite exited 0, reconciled to defaults,
  and the conservative idle profile was reapplied. Current live state is again
  `sched_ext=disabled`, governor `powersave`, EPP `balance_performance`, NVMe `none`,
  and PCIe ASPM `powersave`. Log: `logs/sched_ext_flash_screen_20260710.log`.
- Screened `intel_pstate=passive` in valid run `#72`. It selected scaling driver
  `intel_cpufreq` and governor `schedutil`, scored `136.936 s` (+0.99%), and used
  `3.305 Wh` (+0.13%) with coverage 0.982 and zero drops. Average load power was
  1.11% lower, but longer runtime erased the saving. Stop at N=1 because fixed-work
  energy did not improve. Final GRUB reconcile and reboot passed; verified active
  `intel_pstate`, governor `powersave`, and no passive argument on `/proc/cmdline`.
- Screened the previously untested `EPP=performance` branch in valid run `#73`.
  It scored `135.487 s` (-0.08% versus baseline), used `3.328 Wh` (+0.82%), and
  averaged `80.281 W` (+1.62%), with coverage 0.98 and zero dropped packets.
  Stop at N=1 because the race-to-idle hypothesis did not improve fixed-work energy.
  The suite reboot/reconcile passed and the conservative idle profile was reapplied.
  Log: `logs/epp_performance_screen_20260710.log`.
- Confirmed the previously untested `max_perf_pct=90` point in valid runs `#74`-`#78`.
  N=5 result: `138.469 +/- 0.280 s`, `3.216 +/- 0.005 Wh`, and
  `75.589 +/- 0.190 W`, with coverage >=0.97 and zero drops. It retains 97.92% of
  baseline performance, saves 2.56% fixed-work energy, and lowers average load power
  4.32%, so it is the first load policy to pass the original decision rule. It is
  available as `ansible/profiles/balanced_load.yml`; its apply/read-back and manual
  reset paths were live-tested. The conservative idle profile was reapplied afterward.
  Logs:
  `logs/max_perf_pct_90_screen_20260710.log`,
  `logs/max_perf_pct_90_confirm_20260710.log`, and
  `logs/max_perf_pct_90_phase_c_20260710.log`.

Ansible and suite commands can fail from Codex without a TTY with:

```text
ERROR: Ansible requires blocking IO on stdin/stdout/stderr. Non-blocking file handles detected: <stdin>
```

Use `tty:true` for Ansible and suite runs from Codex.

## Baseline

Valid load baseline runs: `#4`, `#5`, `#6`, `#64`, `#65`.

| Metric | Value |
| --- | ---: |
| Score | 135.590 s |
| Score sd | 0.344 s |
| Energy | 3.301 Wh |
| Energy sd | 0.012 Wh |
| Load power | 79.004 W |
| Peak power | 96.700 W |
| Idle during gated load runs | 4.435 W |
| Sample coverage | 0.984 |
| Dropped packets | 0 |

Valid idle baseline runs: `#1`, `#7`, `#8`, `#62`, `#63`.

| Metric | Value |
| --- | ---: |
| Idle power | 4.355 W |
| Idle power sd | 0.043 W |
| Min idle power | 4.317 W |
| Max idle power | 4.422 W |
| Dropped packets | 0 |

## Phase A Results

Only runs with coverage `>= 0.9` are included. All listed runs had zero dropped packets. Checksum warnings are expected under `--checksum-policy warn`; use dropped packets and coverage to judge quality.

| Variant | n | Score s | vs baseline | Energy Wh | vs baseline | Load W | Fixed-work/J rel | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| baseline | 3 | 135.472 | 0.0% | 3.297 | 0.0% | 78.827 | 1.000 | Reference |
| cpu_governor=powersave | 3 | 135.787 | +0.2% | 3.316 | +0.6% | 73.973 | 0.994 | Baseline is already powersave; one long-tail run affects load W |
| cpu_governor=performance | 3 | 135.359 | -0.1% | 3.339 | +1.3% | 74.765 | 0.987 | No energy win; one long-tail run affects load W |
| turbo=off | 3 | 291.695 | +115.3% | 2.619 | -20.6% | 29.661 | 1.259 | Saves energy but destroys throughput |
| epp=power | 3 | 229.354 | +69.3% | 2.157 | -34.6% | 31.000 | 1.529 | Highest energy saving, too slow for compile workload |
| epp=balance_power | 3 | 147.083 | +8.6% | 3.289 | -0.2% | 70.782 | 1.002 | Nearly no energy win |
| max_perf_pct=80 | 3 | 146.019 | +7.8% | 2.666 | -19.1% | 59.294 | 1.237 | Best load-policy compromise so far, but misses strict 95% perf floor |
| max_perf_pct=70 | 3 | 159.186 | +17.5% | 2.475 | -24.9% | 50.628 | 1.332 | Energy-first, too slow for default |

Strict 95% performance floor from the baseline is `142.6 s` (`135.472 / 0.95`). None of the meaningful energy-saving load knobs met that floor. `max_perf_pct=80` is the best current compromise if the floor can be relaxed; otherwise Phase A did not produce a load default to keep.

The host reports only `performance` and `powersave` CPU governors. `schedutil` was attempted and is unsupported on this host, so exclude it unless the kernel or driver changes.

Two governor runs had unusually long benchmark-phase wall time while PTS score and packet quality remained valid:

- `#11 cpu_governor=powersave`: benchmark phase about 189 s
- `#12 cpu_governor=performance`: benchmark phase about 188 s

Treat the governor energy/load-power averages as less comparable than the PTS scores.

## Commands Used

Completed Phase A sweep:

```bash
python3 run_suite.py 192.168.1.58 \
  --user metrolla \
  --only cpu_governor=performance turbo=off epp= max_perf_pct \
  --tests local/power-bench-build-kernel-defconfig-1.0.0 \
  --repeats 3 \
  --mac 45:AF:4E:55:56:06 \
  --checksum-policy warn \
  --cool-to 55 \
  --skip-baseline
```

Earlier `cpu_governor=powersave` repeat 3 was backfilled manually. Earlier `schedutil` failed because the governor is unavailable.

## Targeted Idle Sweep Results

All included idle rows had zero dropped packets and at least 590 idle samples.
The idle baseline is `4.345 W`. Several variants were stopped after enough
valid idle data showed they were not candidates; only the variants that looked
plausible early received load validation.

| Variant | Idle n | Idle W | vs baseline | Load n | Score s | Energy Wh | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `pcie_aspm=powersave` | 3 | 4.005 | -7.83% | 3 | 135.632 | 3.317 | Best idle result; load energy did not improve |
| `pcie_aspm=powersupersave` | 3 | 4.367 | +0.51% | 3 | 136.365 | 3.325 | Worse than baseline idle |
| `usb_autosuspend` | 3 | 4.377 | +0.73% | 3 | 136.357 | 3.335 | Worse than baseline idle |
| `nic_power_save` | 2 | 4.397 | +1.20% | 0 | - | - | Stopped before load tests |
| `services=trim` | 2 | 4.398 | +1.22% | 0 | - | - | Stopped before load tests |
| `kernel_params=pcie_aspm_force` | 1 | 4.412 | +1.53% | 0 | - | - | Stopped early; GRUB reconciled back to default |
| `sata=med_dipm` | 3 | 4.413 | +1.56% | 0 | - | - | Stopped before load tests |
| `sata=min_power` | 2 | 4.467 | +2.82% | 0 | - | - | Stopped before load tests |

The targeted idle sweep promoted runtime `pcie_aspm=powersave` as the only
plausible idle candidate. Those 3-run values are now superseded by the Phase C
confirmation below: the candidate still helps idle modestly, still misses a 15%
idle-reduction target, and still does not reduce fixed-work compile energy.

`pcie_aspm=force` was explicitly tested as a kernel parameter and was worse than
baseline idle on its first valid run. Do not leave this boot param enabled.

The targeted idle work used these logs:

- `logs/idle_sweep_resume_20260709_1350.log`
- `logs/idle_sweep_remaining_20260709_1630.log`
- `logs/idle_sweep_remaining2_20260709_1657.log`
- `logs/idle_sweep_remaining3_20260709_1722.log`
- `logs/idle_sweep_kernel_aspm_force_20260709_1748.log`

`run_suite.py` now has `--skip-existing`, which skips valid completed rows by
variant, test, repeat, and config hash. Idle validity requires zero dropped
packets and at least 90% of the expected idle samples; load validity requires
zero dropped packets, coverage `>= 0.9`, and a stored score.

## Phase C Confirmation

Completed Phase C confirmation on 2026-07-10 UTC / 2026-07-09 PDT by extending
`pcie_aspm=powersave` and `baseline` to 5 valid repeats each. All included rows
had zero dropped packets. Load rows had coverage `>= 0.98`.

| Variant | Idle n | Idle W | Idle sd | vs baseline | Load n | Score s | Score sd | Energy Wh | Energy sd | Notes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| baseline | 5 | 4.355 | 0.043 | 0.0% | 5 | 135.590 | 0.344 | 3.301 | 0.012 | Reference |
| `pcie_aspm=powersave` | 5 | 4.091 | 0.155 | -6.06% | 5 | 135.849 | 0.384 | 3.318 | 0.010 | Idle-only win; no load-energy win |

Phase C verdict:

- `pcie_aspm=powersave` stays within the strict 95% compile throughput floor:
  `135.849 s` versus the refreshed floor of `142.726 s`
  (`135.590 / 0.95`).
- It reduces idle by about `0.264 W` (`-6.06%`) versus the 5-run baseline.
  This is a real but modest idle-only win, and it still misses a 15% idle
  reduction target.
- It does not reduce fixed-work compile energy: `3.318 Wh` versus baseline
  `3.301 Wh` (`+0.52%`). Do not present it as a load-energy optimization.
- The new idle repeats showed meaningful variance:
  `pcie_aspm=powersave` repeat 4 was `4.344 W`, while repeat 5 was `4.094 W`.
  The larger 5-run idle sd (`0.155 W`) should be mentioned in any report.

Phase C logs and varfiles:

- `logs/phase_c_pcie_aspm_powersave_20260710.log`
- `logs/phase_c_baseline_20260710.log`
- `ansible/vars_phase_c_pcie_aspm_20260710/`
- `ansible/vars_phase_c_baseline_20260710/`

## Idle Diagnosis Pass

Completed a follow-up diagnostic pass on 2026-07-09 PDT / 2026-07-10 UTC.
No new benchmark rows were added. The host was restored afterward to:

- `/proc/cmdline` without `pcie_aspm=force`
- PCIe ASPM policy `[default] performance powersave powersupersave`
- AHCI `0000:00:17.0`, inactive I226 `0000:ab:00.0`, and Wi-Fi
  `0000:ad:00.0` back to runtime PM `control=on`
- inactive I226 and Wi-Fi rebound to `igc` and `iwlwifi`

Read-only diagnostics at baseline showed:

- CPU cores are not the idle blocker: `turbostat` showed around 99.8% CPU C10
  with very low busy time.
- Package idle is the blocker: baseline package residency stayed around 96%
  PC2, with PC6/PC8/PC10 effectively zero.
- Runtime `pcie_aspm=powersave` moves the package mostly into PC6: around
  93-94% PC6, around 2-3% PC2, still zero PC8/PC10.
- This package-residency shift explains the measured `pcie_aspm=powersave`
  wall-power win. It does not appear to be a CPU governor, EPP, or core C-state
  issue.
- Baseline `lspci -vv` already showed ASPM L1 enabled on the main NVMe, I226,
  and Wi-Fi links. The runtime policy changes package residency even though it
  does not unlock PC8/PC10.
- Intel PMC debugfs is available. It showed only shallow S0i2.0 residency in
  this state; S0i2.1 and S0i2.2 residency remained zero.

Active endpoint/runtime-PM map:

- Active SSH route uses `enp172s0` on I226 `0000:ac:00.0`; do not run risky
  runtime-PM, unbind, link-down, or WoL tests against it without console or
  out-of-band access.
- Inactive wired NIC is `enp171s0` on I226 `0000:ab:00.0`.
- Wi-Fi is `wlp173s0f0` on `0000:ad:00.0` and is down.
- Root disk is NVMe `0000:01:00.0`; `nvme_core.default_ps_max_latency_us` is
  already `100000`.

Dead-end probes already tried:

- Set runtime PM `control=auto` on unused AHCI `0000:00:17.0`, inactive I226
  `0000:ab:00.0`, and Wi-Fi `0000:ad:00.0`. The inactive I226 suspended, but
  AHCI and Wi-Fi stayed active, and package residency remained baseline-like
  PC2-only. Do not spend meter time on this as a standalone candidate.
- Combined the same dormant-device `auto` settings with
  `pcie_aspm=powersave`. Residency stayed the same as ASPM powersave alone
  (PC6-only, no PC8/PC10). Do not add this as a stacked candidate.
- Unbound inactive I226 `0000:ab:00.0` from `igc` and Wi-Fi `0000:ad:00.0`
  from `iwlwifi`, leaving active SSH NIC `0000:ac:00.0` bound. Baseline package
  residency still stayed PC2-only. Devices were rebound. Do not pursue this as
  an idle candidate without a separate reason.

## Deeper Idle Diagnosis Pass

Completed a second read-only/temporary-state diagnostic pass on 2026-07-10.
No benchmark or meter rows were added. Temporary debug-latch, ASPM, and NVMe
runtime-PM settings all used exit traps, and the final live state was verified:

- PCIe ASPM policy: `[default] performance powersave powersupersave`
- NVMe PCI endpoint `0000:01:00.0`: `power/control=on` and runtime PM
  `forbidden`
- `/proc/cmdline`: no `pcie_aspm=force`
- PMC LPM latch mode restored to `c10`

Controlled residency samples reproduced the first diagnosis with more exact
numbers:

| State | CPU C10 | Package PC2 | Package PC6 | PC8/PC10 | Package W |
| --- | ---: | ---: | ---: | ---: | ---: |
| baseline ASPM | about 99.8% | 96.1-96.9% | 0% | 0% | 2.22-2.35 |
| `pcie_aspm=powersave` | about 99.8% | 2.2-2.9% | 92.8-94.9% | 0% | 1.35-1.47 |
| powersave + NVMe runtime `auto` | about 99.8% | 2.3-2.9% | 92.8-94.3% | 0% | 1.38-1.47 |

New findings:

- CPU idle is definitively not the blocker. `POLL C1E C6 C10` are enabled on
  every CPU, and aggregate CPU C10 stays around 99.8%.
- Thunderbolt domains and root ports, all USB root hubs and attached devices,
  audio, NPU, and the unused PCIe root ports were runtime-suspended. The iGPU
  was in D3hot with no connected display, about 100% RC6, and no runtime usage.
  Do not pursue these as first-line blockers.
- The root Lexar NVMe path already has ASPM L1.2 enabled at both
  `0000:00:06.0` and `0000:01:00.0`.
  `nvme_core.default_ps_max_latency_us=100000` already permits APST.
- Temporarily setting only NVMe PCI endpoint `power/control=auto` reduced its
  runtime usage count from 2 to 1, but the device remained active. Stacking that
  with ASPM powersave did not change the PC6-only result or unlock PC8. Do not
  spend meter time on this as a candidate.
- The active management path remains the strongest untested hypothesis:
  `enp172s0` uses I226 `0000:ac:00.0` at 2.5 Gb/s, EEE is disabled, and the
  link partner advertises no EEE modes. Root port `0000:00:1c.5` exposes only
  ASPM L1.1 even though the I226 endpoint itself supports L1.2. This is an
  inference, not a confirmed causal result.
- The GEEKOM XT1 Mega is on BIOS 0.67 dated 2025-04-08. GEEKOM's current setup
  guide documents the relevant feature set from BIOS 0.66, so this host is not
  behind that published baseline. Do not flash firmware speculatively.
- Selecting the PMC `S0i2.1` latch produced no completed transition under
  baseline or powersave, so its latched status registers cannot name one
  definitive blocking IP. Live requirement bits are perturbed by the SSH
  request itself and should not be over-interpreted.

The updated theory verdicts are in `theory/pcie_aspm.md`,
`theory/c_states.md`, `theory/disk_power.md`,
`theory/network_power.md`, `theory/usb_autosuspend.md`,
`theory/services.md`, and `theory/kernel_params.md`. Consolidated valid-run
aggregates are exported to `benchmarks/results.csv`.

## Current Deployment and Remaining Work

- A reusable new-node entry point is available at `ansible/run_core_power_bench.yml`.
  It performs idempotent PTS setup and then runs the host-limited, shuffled three-repeat
  core matrix (baseline, `max_perf_pct=90`, and `pcie_aspm=powersave`) through the
  normal meter/DB/reboot harness. Its default matrix contains 15 jobs because baseline
  and ASPM each receive idle plus build measurements. It serializes target nodes,
  requires `power_bench_meter_mac`, writes to the shared DuckDB, and resumes only valid
  rows for the selected host. Use `power_bench_dry_run=true` to preview the matrix
  without a measurement (PTS setup still runs). The default `max_perf_pct=90` variant
  requires Intel pstate; override the variant list on other CPU drivers.

1. Completed: `pcie_aspm=powersave` was selected and deployed as the optional
   conservative idle profile because it satisfies the project's decision rule and
   the 95% compile-throughput floor. It is an idle-only win (`-6.06%`, about
   `0.264 W`). A two-sided Welch test supports the idle difference (`p=0.0167`)
   and finds no compile-time difference (`p=0.2949`), while the small `+0.52%`
   compile-energy regression is statistically detectable (`p=0.0425`). Do not
   present it as a load-energy or 15% idle win. See `FINAL_REPORT.md`.
2. If the target remains a 15% idle reduction, first establish console,
   out-of-band access, or an alternate management interface. Then isolate the
   active I226 path with short residency-only tests: link down, an EEE-capable
   switch/peer, and optionally a 1 Gb/s negotiated link. Do not run these over
   the only SSH path, and do not start meter repeats until one test unlocks PC8
   or produces another clear residency change. The inactive I226 `enp171s0` has
   no cable/carrier. Wi-Fi is driver-bound and can scan nearby 6 GHz networks,
   but has no saved credentials or configured netplan entry. Run the mandatory
   read-only `ansible/preflight_nic_isolation.yml` gate before any active-I226
   experiment; it must prove that the live SSH return path is independent of
   `enp172s0`. The gate was live-tested: it explicitly refuses `enp172s0` on the
   current route and passes for isolated `enp171s0`. Once the gate passes for the
   active NIC, run the paired, meter-free `ansible/diagnose_nic_residency.yml`
   link-down probe; it has a second route check and unconditional restoration.
   The complete sample/change/restore path was live-tested against isolated,
   no-carrier `enp171s0` with short samples. It restored the interface admin-up
   state and the deployed ASPM `powersave` policy. No benchmark or meter rows were
   added.
3. Do not repeat NVMe runtime `auto`, dormant-device runtime-PM, unused-network
   unbind, USB autosuspend, or `pcie_aspm=force` probes. The second diagnosis
   pass closed the NVMe, USB4, display/GPU, audio, and NPU branches.
4. Completed: `max_perf_pct=90` was confirmed to N=5 and exposed as
   `ansible/profiles/balanced_load.yml`. It retains 97.92% of baseline performance,
   saves 2.56% fixed-work energy, and lowers average load power 4.32%, so it passes
   the original 95% floor and decision rule. It is not stacked with the idle profile.
5. If a relaxed performance floor is acceptable for load work, confirm
   `max_perf_pct=80` with another 3 to 5 repeats before treating it as a
   candidate default. Completed on 2026-07-10 PDT with valid runs `#66`, `#67`,
   and `#68`, extending the candidate to N=6. Pooled result: `146.033 +/- 0.137 s`,
   `2.699 +/- 0.036 Wh`, and `59.954 W` average load. It retains 92.85% of baseline
   performance and saves 18.25% energy, so it passes a 90% floor but fails the
   original 95% floor and 20% energy target. It is exposed only as
   `ansible/profiles/relaxed_load.yml`, not the default. Old/new scores were stable;
   the warmer confirmation cohort showed a significant roughly 1.3 W load-power
   drift, so use the pooled N=6 values.
6. Keep using `--checksum-policy warn`, coverage `>= 0.9`, and dropped packets
   `0` as the basic inclusion criteria.

Preview future suite selection first. For idle-targeted variants, do not add
`idle` to `--tests`; `run_suite.py` injects the idle test automatically:

```bash
python3 run_suite.py 192.168.1.58 \
  --user metrolla \
  --only pcie_aspm usb_autosuspend sata= services= nic_power_save \
  --skip-baseline \
  --skip-existing \
  --tests local/power-bench-build-kernel-defconfig-1.0.0 \
  --repeats 3 \
  --list
```
