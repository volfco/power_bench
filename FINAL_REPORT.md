# BLE Wall-Power Optimization Report

## Decision

Keep runtime `pcie_aspm=powersave` as the optional conservative idle profile, and
use `max_perf_pct=90` as the balanced load profile. Both pass the 95% compile-throughput
floor and improve their target metric. ASPM powersave is not a load-energy optimization
and does not achieve the project's aspirational 15% idle reduction.

The profile was applied to the target on 2026-07-10 PDT. Final read-back showed
`default performance [powersave] powersupersave`, with the baseline `powersave`
governor, `balance_performance` EPP, NVMe `none` scheduler, and no
`pcie_aspm=force` kernel argument.

Apply the profile with:

```bash
ansible-playbook -i ansible/hosts ansible/apply_optimizations.yml \
  -e @ansible/profiles/conservative_idle.yml
```

It is runtime-only and resets on reboot. To revert without reboot:

```bash
ansible-playbook -i ansible/hosts ansible/reset_to_baseline.yml
```

## Confirmed result

Phase C used five valid repeats per configuration. All included rows had zero
dropped packets; load coverage was at least 0.98.

| Metric | Baseline | Conservative idle | Delta | Welch p-value |
| --- | ---: | ---: | ---: | ---: |
| Stable idle wall power | 4.355 +/- 0.043 W | 4.091 +/- 0.155 W | -6.06% | 0.0167 |
| Kernel compile time | 135.590 +/- 0.344 s | 135.849 +/- 0.384 s | +0.19% | 0.2949 |
| Compile energy | 3.301 +/- 0.012 Wh | 3.318 +/- 0.010 Wh | +0.52% | 0.0425 |

The refreshed 95% performance floor is 142.726 seconds. The tuned mean is well
inside it. The idle benefit is about 0.264 W and is statistically supported, while
compile performance is indistinguishable. The small compile-energy change goes in
the wrong direction and is statistically detectable, so this setting is suitable
only when idle savings matter more than the roughly 0.017 Wh added to this compile.

## Balanced-load profile

`ansible/profiles/balanced_load.yml` caps Intel pstate maximum performance at 90%.
Five valid repeats produced `138.469 +/- 0.280 s`, `3.216 +/- 0.005 Wh`, and
`75.589 +/- 0.190 W`, versus baseline `135.590 +/- 0.344 s`, `3.301 +/- 0.012 Wh`,
and `79.004 W`. It retains 97.92% of baseline performance, reduces fixed-work
energy by 2.56%, and lowers average load power by 4.32%. Compile time and energy
both differ clearly from baseline (Welch `p=7.3e-7` and `p=1.8e-5`).

This is the preferred load profile when the original 95% floor applies:

```bash
ansible-playbook -i ansible/hosts ansible/apply_optimizations.yml \
  -e @ansible/profiles/balanced_load.yml
```

It is runtime-only and returns to the baseline 100% ceiling after reboot. It has
not been stack-tested with the conservative idle profile.

## Optional relaxed-load profile

`ansible/profiles/relaxed_load.yml` caps Intel pstate maximum performance at 80%.
Six valid repeats produced `146.033 +/- 0.137 s` and `2.699 +/- 0.036 Wh`, versus
baseline `135.590 +/- 0.344 s` and `3.301 +/- 0.012 Wh`. It retains 92.85% of
baseline performance while reducing fixed-work energy by 18.25% and average load
power by 24.11%.

This passes a 90% performance floor but fails the original 95% floor and 20% energy
target. Use it only when that tradeoff is explicit:

```bash
ansible-playbook -i ansible/hosts ansible/apply_optimizations.yml \
  -e @ansible/profiles/relaxed_load.yml
```

Do not stack it with the conservative idle profile for load work: ASPM powersave
slightly increased compile energy in its own confirmation.

## Mechanism

The CPU cores already spend about 99.8% of idle time in C10. The package, however,
stays about 96-97% in PC2 under the default ASPM policy. `powersave` shifts it to
about 93-95% PC6 and lowers observed package power from roughly 2.2-2.35 W to
1.35-1.47 W. It does not unlock PC8 or PC10.

## Rejected or deferred paths

The CPU governor, turbo, EPP, more aggressive maximum-performance caps, ASPM
`powersupersave`, USB autosuspend, SATA link policies, service trimming, generic NIC
power saving, NVMe `mq-deadline`, and `pcie_aspm=force` did not meet the decision rule.
The one-run `EPP=performance` screen matched baseline compile time (-0.08%) but used
0.82% more energy, so it was not promoted to more repeats.
`mq-deadline` was 0.31% slower and used 0.56% more energy than baseline `none`, with
both changes inside run noise. `intel_pstate=passive` selected `intel_cpufreq` plus
`schedutil`; its one-run screen was 0.99% slower and used 0.13% more fixed-work energy,
so it was not promoted to more repeats. In particular,
`max_perf_pct=80` is available only as the optional relaxed-load profile described
above; it does not meet the original project targets.

The sched_ext screen also produced no load candidate. One valid repeat each of LAVD
powersave/performance, BPFLand powersave, and Rusty retained only 91.23%, 91.09%,
86.98%, and 82.11% of baseline compile performance, respectively; none improved
fixed-work energy decisively. The additional `scx_flash` v1.1.1 screen retained 98.32%
of baseline performance (`137.912 s`) but used 2.57% more fixed-work energy (`3.386 Wh`),
so it was also rejected after one valid repeat (98.47% coverage, zero drops). Tickless
v1.1.1 self-ejected at attach on this kernel with `starting timer on cpu8, which is not
a scheduling CPU`, so it was not benchmarked.

The leading untested PC8-blocker hypothesis is the active 2.5 Gb/s Intel I226 path.
It is also the only SSH management path, and its peer does not advertise EEE. Do not
alter it remotely. Testing link-down, an EEE-capable peer, or a forced 1 Gb/s link
requires console/out-of-band access or another management interface. No meter repeats
should be started until a short residency test shows a meaningful package-state change.

The consolidated aggregates are in `benchmarks/results.csv`; detailed rationale is
in `theory/`, especially `theory/pcie_aspm.md` and `theory/network_power.md`.

## Combined (stacked) configurations

Phase D stacks the top OFAT winners into a single boot to measure additive or
interactive effects. Every combined variant is defined in
[`run_suite.py`](run_suite.py) — the `EXPERIMENTS` list (Intel, line 84) and
`AMD_EXPERIMENTS` list (AMD, line 206) — as a tuple of
`(label, {knob_overrides}, target)`. Each override key maps to an Ansible variable
consumed by [`ansible/apply_optimizations.yml`](ansible/apply_optimizations.yml);
kernel params are applied via a managed GRUB fragment and require a reboot.

### Intel combined stacks

All Intel stacks use the `core` sweep profile and target the Intel-specific knobs
(`intel_pstate` EPP, `max_perf_pct`). Kernel-param variants require a reboot to
apply and clear.

| # | Label | Knobs changed | Reboot | Rationale |
|---|-------|---------------|--------|-----------|
| 1 | `combined=mitigations+nokaslr+turbo_off+pcie_aspm+pstate90` | `mitigations=off` + `nokaslr` + `turbo=off` + `pcie_aspm=powersave` + `max_perf_pct=90` | yes | Full 5-knob stack without governor override — keeps default `powersave` governor. Tests kernel hardening removal + turbo + ASPM + conservative pstate cap. |
| 2 | `combined=mitigations+nokaslr+turbo_off+pcie_aspm+governor_powersave` | `mitigations=off` + `nokaslr` + `turbo=off` + `pcie_aspm=powersave` + `cpu_governor=powersave` | yes | Drops `max_perf_pct` in favor of governor-level power saving. Tests whether governor and pstate cap overlap or complement. |
| 3 | `combined=mitigations+nokaslr+turbo_off+pstate90+governor_powersave` | `mitigations=off` + `nokaslr` + `turbo=off` + `max_perf_pct=90` + `cpu_governor=powersave` | yes | Drops PCIe ASPM to isolate kernel + turbo + pstate + governor interaction without idle bus power changes. |
| 4 | `combined=mitigations+nokaslr+turbo_off+pcie_aspm` | `mitigations=off` + `nokaslr` + `turbo=off` + `pcie_aspm=powersave` | yes | Lightweight 4-knob stack: kernel + turbo + ASPM only. No pstate cap or governor change — baseline for comparison against the full stacks. |
| 5 | `combined=all_five` | `mitigations=off` + `nokaslr` + `turbo=off` + `pcie_aspm=powersave` + `max_perf_pct=90` + `cpu_governor=powersave` | yes | Maximum overlap: all six knobs together. Tests whether the cumulative effect exceeds the sum of individual wins, or whether knobs cancel. |
| 6 | `combined=turbo_off+pcie_aspm+pstate90+governor_powersave` | `turbo=off` + `pcie_aspm=powersave` + `max_perf_pct=90` + `cpu_governor=powersave` | no | Production-safe (no kernel params). Four runtime knobs — the heaviest stack deployable without a reboot. |
| 7 | `combined=turbo_off+pcie_aspm+pstate90` | `turbo=off` + `pcie_aspm=powersave` + `max_perf_pct=90` | no | Minimal production stack: turbo + ASPM + pstate cap. No governor override, no kernel params. |
| 8 | `combined=turbo_off+governor_powersave+pcie_aspm+gpu_low` | `turbo=off` + `cpu_governor=powersave` + `pcie_aspm=powersave` + `gpu_power_profile=low` | no | Production-safe with GPU power management added. Tests whether GPU power profile stacks with the core wins. |

Knob details (links to implementation):
- `mitigations=off` + `nokaslr` — kernel boot params, applied via GRUB fragment:
  [`ansible/optimizations/kernel_params.yml`](ansible/optimizations/kernel_params.yml)
- `turbo=off` — `intel_pstate/no_turbo=1`:
  [`ansible/optimizations/turbo_boost.yml`](ansible/optimizations/turbo_boost.yml)
- `pcie_aspm=powersave` — PCIe link power management:
  [`ansible/optimizations/pcie_aspm.yml`](ansible/optimizations/pcie_aspm.yml)
- `max_perf_pct=90` — Intel pstate maximum performance cap:
  [`ansible/optimizations/p_states.yml`](ansible/optimizations/p_states.yml)
- `cpu_governor=powersave` — CPU frequency governor:
  [`ansible/optimizations/cpu_governor.yml`](ansible/optimizations/cpu_governor.yml)
- `gpu_power_profile=low` — GPU runtime power management:
  [`ansible/optimizations/gpu_power.yml`](ansible/optimizations/gpu_power.yml)

### AMD combined stacks

All AMD stacks use the `amd` sweep profile. AMD has no `intel_pstate` EPP or
`max_perf_pct`; the 5th knob is `gpu_power_profile=low` instead. Kernel-param
variants require a reboot.

| # | Label | Knobs changed | Reboot | Rationale |
|---|-------|---------------|--------|-----------|
| 1 | `combined=mitigations+nokaslr+turbo_off+pcie_aspm+governor_powersave` | `mitigations=off` + `nokaslr` + `turbo=off` + `pcie_aspm=powersave` + `cpu_governor=powersave` | yes | Full AMD stack: kernel + turbo + ASPM + governor. No GPU power to keep the test focused on CPU/PCIe. |
| 2 | `combined=mitigations+nokaslr+turbo_off+pcie_aspm+gpu_low` | `mitigations=off` + `nokaslr` + `turbo=off` + `pcie_aspm=powersave` + `gpu_power_profile=low` | yes | Swaps governor for GPU power — tests whether GPU adds idle savings on top of the CPU/PCIe stack. |
| 3 | `combined=mitigations+nokaslr+turbo_off+governor_powersave+gpu_low` | `mitigations=off` + `nokaslr` + `turbo=off` + `cpu_governor=powersave` + `gpu_power_profile=low` | yes | Drops PCIe ASPM to isolate kernel + turbo + governor + GPU interaction. |
| 4 | `combined=mitigations+nokaslr+pcie_aspm+governor_powersave+gpu_low` | `mitigations=off` + `nokaslr` + `pcie_aspm=powersave` + `cpu_governor=powersave` + `gpu_power_profile=low` | yes | Drops turbo to measure the remaining four knobs without boost control. |
| 5 | `combined=all_five` | `mitigations=off` + `nokaslr` + `turbo=off` + `pcie_aspm=powersave` + `cpu_governor=powersave` + `gpu_power_profile=low` | yes | Maximum overlap on AMD: all six knobs together. |
| 6 | `combined=turbo_off+pcie_aspm+governor_powersave+gpu_low` | `turbo=off` + `pcie_aspm=powersave` + `cpu_governor=powersave` + `gpu_power_profile=low` | no | Production-safe (no kernel params). Four runtime knobs — heaviest AMD stack deployable without reboot. |
| 7 | `combined=turbo_off+pcie_aspm+governor_powersave` | `turbo=off` + `pcie_aspm=powersave` + `cpu_governor=powersave` | no | Minimal production stack: turbo + ASPM + governor. No GPU, no kernel params. |
| 8 | `combined=turbo_off+governor_powersave+gpu_low` | `turbo=off` + `cpu_governor=powersave` + `gpu_power_profile=low` | no | Drops PCIe ASPM to test CPU + GPU stack alone in production. |

AMD knob details:
- `turbo=off` — `cpufreq/boost=0` (AMD acpi-cpufreq):
  [`ansible/optimizations/turbo_boost.yml`](ansible/optimizations/turbo_boost.yml)
- `cpu_governor=powersave`:
  [`ansible/optimizations/cpu_governor.yml`](ansible/optimizations/cpu_governor.yml)
- `pcie_aspm=powersave`:
  [`ansible/optimizations/pcie_aspm.yml`](ansible/optimizations/pcie_aspm.yml)
- `gpu_power_profile=low`:
  [`ansible/optimizations/gpu_power.yml`](ansible/optimizations/gpu_power.yml)
- `mitigations=off` + `nokaslr` — kernel boot params:
  [`ansible/optimizations/kernel_params.yml`](ansible/optimizations/kernel_params.yml)

### How to run a combined stack

List all combined variants for a host:

```bash
python run_suite.py node2 --list --sweep core  # Intel
python run_suite.py node2 --list --sweep amd   # AMD
```

Run a specific combined stack (3 repeats by default):

```bash
python run_suite.py node2 --only combined=all_five \
  --mac <MAC> --checksum-policy warn --cool-to 55
```

Or do a dry run first:

```bash
python run_suite.py node2 --only combined=all_five --dry-run
```
