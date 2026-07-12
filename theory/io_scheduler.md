# I/O scheduler

## Hypothesis
The block I/O scheduler mostly affects throughput/latency, not power directly, but it can
move **energy-to-complete** on I/O-bound work: `none` minimizes CPU overhead on fast
NVMe/SSD (less scheduling work → less energy), while `bfq`/`mq-deadline` help on slower
or contended devices. Expect a small effect, concentrated on the disk test.

## Implementation
- Playbook: `ansible/optimizations/io_scheduler.yml`, var `io_scheduler`.
- Apply: `ansible-playbook -i ansible/hosts ansible/optimizations/io_scheduler.yml -e io_scheduler=none`
- Revert: `reset_to_baseline.yml` (`none` on this NVMe rig)
- Verify: per-device scheduler reported by the playbook.

## Measurement (mean ± stdev over N repeats, all repeats counted)
| Metric                  | Baseline (`none`) | `mq-deadline` | Delta |
|-------------------------|------------------:|--------------:|------:|
| Energy-to-complete (Wh) | 3.301 +/- 0.012 | 3.320 +/- 0.018 | +0.56% |
| Compile score (s)       | 135.590 +/- 0.344 | 136.016 +/- 0.133 | +0.31% slower |
| Avg load power (W)      | 79.004 +/- 0.522 | 79.515 +/- 0.425 | +0.65% |
| Peak load power (W)     | 96.700 +/- 0.158 | 96.633 +/- 0.231 | -0.07% |

Baseline is N=5; `mq-deadline` is N=3 (runs 69-71). Every included run had coverage
>=0.98 and zero dropped packets. The candidate stayed inside the 95% performance
floor, but none of its changes were significant in two-sided Welch tests: score
`p=0.051`, energy `p=0.206`, load power `p=0.190`.

Test(s): disk (primary)   N: ...   Run order: randomized   Ambient: ... °C

## Analysis
The root device is NVMe and exposes only `none` and `mq-deadline`; `bfq` and `kyber`
are unsupported and must not be scheduled. Baseline already uses `none`.
`mq-deadline` did not improve the fixed-work compile result: its mean score, energy,
and load power all moved slightly in the wrong direction, and the differences were
within run noise. Keep the lower-overhead NVMe default and do not spend more meter
time on this scheduler for the current workload.

## Verdict
- [ ] Keep   - [x] Revert   - [ ] Refine: retain baseline `none`; reject
  `mq-deadline` for this NVMe compile workload.
