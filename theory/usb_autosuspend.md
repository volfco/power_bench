# USB autosuspend

## Hypothesis
Letting idle USB devices autosuspend saves a small amount of idle power, with a risk
that flaky peripherals misbehave after resume.

## Implementation
- Playbook: `ansible/optimizations/usb_autosuspend.yml`, vars `usb_autosuspend`, `usb_autosuspend_delay_ms`.
- Apply with `usb_autosuspend=true`.
- Revert: `reset_to_baseline.yml` (control=on)
- The meter's BLE adapter is on the control machine, not the target.

## Measurement

| Metric | Baseline | Autosuspend | Delta |
|--------|---------:|------------:|------:|
| Idle power, N=3 | 4.345 W | 4.377 +/- 0.038 W | +0.73% |
| Compile score, N=3 | 135.472 s | 136.357 +/- 0.249 s | +0.65% slower |
| Energy-to-complete, N=3 | 3.297 Wh | 3.335 +/- 0.003 Wh | +1.15% |
| Avg load power | 78.827 W | 79.549 W | +0.92% |

## Analysis
The target's internal USB hubs, Bluetooth device, and all four root hubs were observed
runtime-suspended during the follow-up diagnosis. The explicit sweep did not improve
idle and slightly regressed the compile measurements, so there is no hidden USB
candidate to carry forward.

## Verdict
- [ ] Keep   - [x] Revert   - [ ] Refine: reject for this target.
