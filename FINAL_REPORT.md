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
