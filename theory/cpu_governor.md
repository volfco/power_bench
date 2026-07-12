# CPU governor

## Hypothesis
The governor sets the frequency-selection policy and is the single biggest power/perf
lever. `powersave` (intel_pstate) or `schedutil` should cut idle and load power
substantially; the risk is a single-thread / latency regression on bursty work. Expect
a clear perf-per-joule win on sustained loads, a possible score drop near the floor on
latency-sensitive tests.

## Implementation
- Playbook: `ansible/optimizations/cpu_governor.yml`, var `cpu_governor`.
- Apply: `ansible-playbook -i ansible/hosts ansible/optimizations/cpu_governor.yml -e cpu_governor=powersave`
- Revert: `reset_to_baseline.yml`
- Verify: `cat /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor`

Available governors on this host: `performance powersave`. `schedutil` is unavailable.

## Measurement

Compile test: `local/power-bench-build-kernel-defconfig-1.0.0`. Values are means over
N=3 valid repeats with coverage >=0.9 and zero dropped packets.

| Variant | Score s | Score sd | Energy Wh | Energy sd | Load W | Peak W | Fixed-work/J rel | Notes |
|---------|--------:|---------:|----------:|----------:|-------:|-------:|-----------------:|-------|
| Baseline | 135.472 | 0.396 | 3.297 | 0.014 | 78.827 | 96.600 | 1.000 | Baseline governor is already `powersave` |
| cpu_governor=powersave | 135.787 | 0.233 | 3.316 | 0.030 | 73.973 | 96.433 | 0.994 | One long-tail run affects energy/load comparison |
| cpu_governor=performance | 135.359 | 0.455 | 3.339 | 0.038 | 74.765 | 96.567 | 0.987 | One long-tail run affects energy/load comparison |
| cpu_governor=schedutil | n/a | n/a | n/a | n/a | n/a | n/a | n/a | Unsupported by this host |

## Analysis
This host's baseline is already `powersave`, so explicitly setting `powersave` is a
near no-op. `performance` produced no useful throughput win and consumed 1.3% more
energy than baseline.

Runs #11 (`powersave`) and #12 (`performance`) had unusually long benchmark-phase wall
time, about 188 to 189 s versus about 150 s for normal compile runs, while the PTS score,
coverage, and dropped-packet checks remained valid. Treat the governor score comparison
as valid, but treat the load-power and energy averages as less clean than the P-state
and turbo runs.

Because `schedutil` is not available, there is no governor alternative worth carrying
forward on this kernel.

## Verdict
- [ ] Keep   - [x] Revert   - [ ] Refine: keep the host baseline governor state and do
  not include governor switching in the default profile.
