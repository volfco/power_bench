# Kernel boot parameters

## Hypothesis
Some power levers are only available at boot. Each parameter must be tested
individually because bundling parameters destroys attribution.

## Implementation - REBOOT REQUIRED
- Playbook: `ansible/optimizations/kernel_params.yml`, var `kernel_params`.
- Apply example: `-e '{"kernel_params":["pcie_aspm=force"]}'`
- Revert: `-e '{"kernel_params":[]}'`
- Verify `/proc/cmdline` after reboot.

## Measurement

`pcie_aspm=force` produced 4.412 W on its first valid idle run versus the 4.345 W
screening baseline, a 1.53% regression. The branch was stopped before load testing.
The managed GRUB fragment was reconciled afterward and `/proc/cmdline` was verified
without the parameter.

`intel_pstate=passive` was screened with valid run 72. It switched the captured scaling
driver from `intel_pstate` to `intel_cpufreq` and selected `schedutil`, as hypothesized.
The result was 136.936 s and 3.305 Wh, versus the N=5 baseline means of 135.590 s and
3.301 Wh: 0.99% slower with 0.13% more energy. Average load power fell 1.11% to
78.127 W, but the longer runtime erased that secondary power reduction. Coverage was
0.982 with zero dropped packets. Because there was no fixed-work energy win, the branch
was stopped at one run rather than spending two more repeats on a non-candidate.

## Analysis
The firmware already permits OS ASPM control and the relevant NVMe path exposes L1.2;
forcing ASPM does not solve the missing package PC8 state. Do not repeat this probe.

The managed GRUB fragment was cleared after the probe, the target rebooted, and final
read-back confirmed `intel_pstate` active mode, governor `powersave`, and no
`intel_pstate=passive` argument. Do not repeat this parameter for the current workload.

## Verdict
- `pcie_aspm=force` - [ ] Keep   [x] Revert   [ ] Refine: measured worse than
  baseline and confirmed absent after restore.
- `intel_pstate=passive` - [ ] Keep   [x] Revert   [ ] Refine: passes the performance
  floor but does not reduce fixed-work energy; confirmed absent after restore.
