# Turbo boost

## Hypothesis
Turbo/boost trades large power increases for the last few percent of frequency, so
disabling it should improve perf-per-joule on sustained, fully-loaded workloads (the
extra wattage buys little extra work). Risk: burst/latency-bound tests lose throughput
and may approach the performance floor. Little idle-power effect (turbo is a load state).

## Implementation
- Playbook: `ansible/optimizations/turbo_boost.yml`, var `turbo_enabled`.
- Apply (disable): `ansible-playbook -i ansible/hosts ansible/optimizations/turbo_boost.yml -e turbo_enabled=false`
- Revert: `reset_to_baseline.yml`
- Verify: `cat /sys/devices/system/cpu/intel_pstate/no_turbo` (0=on, 1=off) or `.../cpufreq/boost`

## Measurement

Compile test: `local/power-bench-build-kernel-defconfig-1.0.0`. Values are means over
N=3 valid repeats with coverage >=0.9 and zero dropped packets.

| Metric | Baseline, turbo on | Turbo off | Delta |
|--------|-------------------:|----------:|------:|
| Score | 135.472 s | 291.695 s | +115.3% slower |
| Score sd | 0.396 s | 0.728 s | |
| Energy-to-complete | 3.297 Wh | 2.619 Wh | -20.6% |
| Energy sd | 0.014 Wh | 0.003 Wh | |
| Avg load power | 78.827 W | 29.661 W | -62.4% |
| Peak power | 96.600 W | 36.267 W | -62.5% |
| Fixed-work/J relative | 1.000 | 1.259 | +25.9% |
| 95% floor | Pass | Fail | floor is 142.6 s |

## Analysis
Disabling turbo produced the expected large power reduction and did improve
fixed-work/J. It is not a viable default for this compile workload because the runtime
more than doubled, far below the 95% performance floor.

This knob may still be useful for energy-first background jobs where wall time is much
less important than peak power or thermal output, but it should not be stacked into the
default profile without an explicit energy-first mode.

## Verdict
- [ ] Keep   - [x] Revert   - [ ] Refine: reject for the default compile profile.
