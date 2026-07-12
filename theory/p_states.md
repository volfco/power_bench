# P-states (intel_pstate / HWP)

## Hypothesis
The HWP energy-performance preference (EPP) biases the hardware governor toward power or
performance without changing the governor, and `max_perf_pct` caps the frequency ceiling.
`EPP=power` plus a modest frequency cap should cut load power with a small score cost —
finer-grained than the governor switch, so useful for tuning the perf/joule knee.

## Implementation
- Playbook: `ansible/optimizations/p_states.yml`, vars `energy_perf_preference`, `pstate_max_perf_pct`, `pstate_min_perf_pct`.
- Apply: `ansible-playbook -i ansible/hosts ansible/optimizations/p_states.yml -e energy_perf_preference=power -e pstate_max_perf_pct=80`
- Revert: `reset_to_baseline.yml` (EPP=default, max_perf_pct=100)
- Verify: EPP / max_perf_pct / min_perf_pct reported by the playbook.

## Measurement

Compile test: `local/power-bench-build-kernel-defconfig-1.0.0`. The baseline is N=5;
`max_perf_pct=90` is confirmed with N=5 and `max_perf_pct=80` with N=6. All included
runs had coverage >=0.97 and zero dropped packets. `EPP=performance` has one valid
screening run; the other EPP variants remain Phase A N=3 results.

| Variant | Score s | Score sd | Energy Wh | Energy sd | Load W | Peak W | Fixed-work/J rel | 95% floor |
|---------|--------:|---------:|----------:|----------:|-------:|-------:|-----------------:|-----------|
| Baseline | 135.590 | 0.344 | 3.301 | 0.012 | 79.004 | 96.700 | 1.000 | Pass |
| EPP=performance (N=1) | 135.487 | - | 3.328 | - | 80.281 | 96.400 | 0.992 | Pass |
| EPP=power | 229.354 | 0.167 | 2.157 | 0.004 | 31.000 | 39.333 | 1.529 | Fail |
| EPP=balance_power | 147.083 | 0.540 | 3.289 | 0.006 | 70.782 | 96.500 | 1.002 | Fail |
| max_perf_pct=90 | 138.469 | 0.280 | 3.216 | 0.005 | 75.589 | 95.720 | 1.026 | Pass |
| max_perf_pct=80 | 146.033 | 0.137 | 2.699 | 0.036 | 59.954 | 75.983 | 1.223 | Fail |
| max_perf_pct=70 | 159.186 | 0.734 | 2.475 | 0.008 | 50.628 | 63.133 | 1.332 | Fail |

Strict 95% performance floor: `142.726 s`. A relaxed 90% floor is `150.656 s`.

## Analysis
The hypothesis was partly right: HWP/EPP and the max performance cap can pull load
power down substantially. The cost is larger than the original "small score cost"
assumption for this compile workload.

`EPP=power` is the most energy-efficient single knob by fixed-work/J, cutting energy by
34.6%, but it makes the build 69.3% slower. `EPP=balance_power` is not useful here:
it is 8.6% slower with only a 0.2% energy reduction.

The previously untested `max_perf_pct=90` point is the first load policy to satisfy
the original decision rule. Across runs #74-#78 it retained 97.92% of baseline
performance (`138.469 +/- 0.280 s`), reduced energy 2.56% (`3.216 +/- 0.005 Wh`),
and reduced average load power 4.32% (`75.589 +/- 0.190 W`). The compile-time and
energy differences are statistically clear (two-sided Welch `p=7.3e-7` and
`p=1.8e-5`, respectively). It is exposed as `ansible/profiles/balanced_load.yml`.

The confirmed `max_perf_pct=80` result is the stronger energy/performance tradeoff
for a relaxed floor: it retains 92.85% of baseline
performance (7.70% slower), reduces energy by 18.25%, and lowers average load power by
24.11%. It passes a 90% performance floor but still misses both the original 95% floor
and the project's 20% fixed-work energy target. `max_perf_pct=70` saves a little more
energy than 80%, but the extra slowdown is too high for a default compile profile.

The missing `EPP=performance` branch was screened in valid run #73 (coverage 0.98,
zero dropped packets). It was effectively tied on compile time (-0.08%) but used
0.82% more energy and raised average load power 1.62%. The race-to-idle hypothesis
therefore did not produce a fixed-work energy win, so the branch stopped at N=1.

The three original and three confirmation scores are indistinguishable (146.019 versus
146.047 s, Welch `p=0.836`). Energy and load power were higher in the confirmation
cohort (2.731 versus 2.666 Wh, `p=0.005`; 60.615 versus 59.294 W, `p=0.006`). The
captured configuration was identical, while meter ambient rose from 28-29 C to 31-32 C
and CPU start temperature rose in two runs. This is consistent with a roughly 1.3 W
thermal/fan or environmental drift, so use the more conservative pooled N=6 result.

## Verdict
- [x] Keep   - [ ] Revert   - [ ] Refine: use `max_perf_pct=90` as the confirmed
  `ansible/profiles/balanced_load.yml` profile; it passes the original 95% floor and
  improves fixed-work energy. Keep `max_perf_pct=80` only as the optional
  `ansible/profiles/relaxed_load.yml` profile for users who explicitly accept a 90%
  floor. Do not keep `EPP=performance`,
  `EPP=power`, `EPP=balance_power`, or `max_perf_pct=70` as defaults for this workload.
