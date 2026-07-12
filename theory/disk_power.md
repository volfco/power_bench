# Disk power

## Hypothesis
SATA link power management lets idle SATA links enter low-power states. NVMe APST and
PCIe L1 substates are the corresponding root-disk mechanisms.

## Implementation
- Playbook: `ansible/optimizations/disk_power.yml`, vars `sata_link_pm`, `hdd_apm_level`.
- Apply: `ansible-playbook -i ansible/hosts ansible/optimizations/disk_power.yml -e sata_link_pm=med_power_with_dipm`
- Revert: `reset_to_baseline.yml` (max_performance)
- Verify: link power policy reported by the playbook.

## Measurement

The targeted idle screen used the contemporaneous 4.345 W baseline. All included
runs had zero dropped packets and at least 590 stable idle samples.

| Variant | N | Idle power | Delta vs baseline |
|---------|--:|-----------:|------------------:|
| Baseline | 3 | 4.345 W | 0.0% |
| `med_power_with_dipm` | 3 | 4.413 +/- 0.038 W | +1.56% |
| `min_power` | 2 | 4.467 +/- 0.010 W | +2.82% |

## Analysis
The root disk is NVMe and the unused AHCI controller did not produce a power win.
The NVMe/root-port path already has ASPM L1.2 enabled and
`nvme_core.default_ps_max_latency_us=100000`. Temporarily setting PCI endpoint
`0000:01:00.0` to runtime `auto` left it active with usage count 1 and did not unlock
package PC8 when stacked with ASPM `powersave`. Do not add that as a meter candidate.

## Verdict
- [ ] Keep   - [x] Revert   - [ ] Refine: reject both SATA policies and the NVMe
  runtime-PM probe for this host.
