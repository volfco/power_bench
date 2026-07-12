# GPU power

## Hypothesis
Forcing a low GPU power profile (amdgpu `power_dpm_state=battery`/`force_performance_level=low`,
NVIDIA reduced power limit) caps GPU draw. On a CPU-bound benchmark box this mostly affects
**idle** power (the GPU is already gated); it only matters under load if a test exercises the
GPU. Risk: throttles any GPU-bound workload.

## Implementation
- Playbook: `ansible/optimizations/gpu_power.yml`, var `gpu_power_profile` (low | auto).
- Apply: `ansible-playbook -i ansible/hosts ansible/optimizations/gpu_power.yml -e gpu_power_profile=low`
- Revert: `ansible-playbook -i ansible/hosts ansible/optimizations/gpu_power.yml -e gpu_power_profile=auto`
- Verify: driver-specific; check `power_dpm_force_performance_level` (amdgpu) or `nvidia-smi -q -d POWER`.

## Measurement (mean ± stdev over N repeats, all repeats counted)
| Metric         | Baseline | profile=low | Δ |
|----------------|----------|-------------|---|
| Idle power (W) |          |             |   |
| Avg load power (W) |      |             |   |
| Benchmark result |        |             |   |

Test(s): ...   N: ...   Run order: randomized   Ambient: ... °C

## Analysis
On a headless/CPU box, expect a small idle delta and no load change. Only meaningful with
an actual GPU workload — note the GPU model and driver. Not reverted by reset_to_baseline.

## Verdict
- [ ] Keep   - [ ] Revert   - [ ] Refine: ___
