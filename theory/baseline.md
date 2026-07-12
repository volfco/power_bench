# Baseline

The reference state every other optimization is measured against. Capture once on a
freshly-reset, thermally-cold host; re-capture if the kernel or hardware changes.

## Capture

```bash
ansible-playbook -i ansible/hosts ansible/reset_to_baseline.yml
ansible-playbook -i ansible/hosts ansible/optimizations/baseline.yml   # prints stock config
```

## Stock configuration (observed)

- Kernel: `7.0.0-27-generic`
- CPU model / cores: `Intel(R) Core(TM) Ultra 5 125H`
- Governor / available governors: current `powersave`
- Turbo (intel no_turbo / boost): on (`intel_pstate/no_turbo=0`)
- EPP: `balance_performance`
- C-states present: `POLL C1E C6 C10`; all are enabled on every CPU
- PCIe ASPM policy: `[default] performance powersave powersupersave`
- I/O scheduler: `nvme0n1 [none] mq-deadline`
- sched_ext state: `none`
- Running services:
- `/proc/cmdline`: `BOOT_IMAGE=/vmlinuz-7.0.0-27-generic root=/dev/mapper/ubuntu--vg-ubuntu--lv ro crashkernel=2G-4G:320M,4G-32G:512M,32G-64G:1024M,64G-128G:2048M,128G-:4096M`
- Ambient temperature (C): about `27.0` from meter temperature during baseline runs
- Meter device type (AC/DC/USB): AC mains meter, BLE MAC `45:AF:4E:55:56:06`

## Reference measurements

Run each test in the suite and record the baseline numbers other experiments compare to.
The Phase C reference has N=5 for the long-idle test and N=5 for the kernel
compile load test.

| Test | Runs | N | Metric | Mean | Stdev | Min | Max | Coverage / drops |
|------|------|---:|--------|-----:|------:|----:|----:|------------------|
| `idle` | #1, #7, #8, #62, #63 | 5 | Idle power | 4.355 W | 0.043 W | 4.317 W | 4.422 W | 0 drops |
| `local/power-bench-build-kernel-defconfig-1.0.0` | #4, #5, #6, #64, #65 | 5 | Score | 135.590 s | 0.344 s | n/a | n/a | 0.984 avg / 0 drops |
| `local/power-bench-build-kernel-defconfig-1.0.0` | #4, #5, #6, #64, #65 | 5 | Energy | 3.301 Wh | 0.012 Wh | n/a | n/a | 0.984 avg / 0 drops |
| `local/power-bench-build-kernel-defconfig-1.0.0` | #4, #5, #6, #64, #65 | 5 | Avg load power | 79.004 W | n/a | n/a | n/a | 0.984 avg / 0 drops |
| `local/power-bench-build-kernel-defconfig-1.0.0` | #4, #5, #6, #64, #65 | 5 | Peak power | 96.700 W | n/a | n/a | n/a | 0.984 avg / 0 drops |

Optional coverage remains open for non-compile workloads:

| Test | Status |
|------|--------|
| `pts/llama-cpp` | Optional; current target lacks required disk footprint |
| `pts/disk` | Optional future I/O coverage |
| `pts/memory` | Optional future memory coverage |

## Notes

These reference values back the success criteria in `plan.md` (idle -15 %, perf floor
95 %, perf/joule +20 %). The refreshed strict 95 % performance floor for the compile
benchmark is `142.726 s` (`135.590 / 0.95`). Keep the room ambient and PSU/cabling identical for every
later run.

Use `--checksum-policy warn` on this meter. Runs #2 and #3 in the current DB are invalid
setup attempts (`bench_sample_coverage=0`) and should not be used as baseline data.
