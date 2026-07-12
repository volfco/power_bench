# PCIe ASPM

## Hypothesis
PCIe Active State Power Management lets idle PCIe links drop into low-power L0s/L1 states,
saving **idle** bus power (NVMe, NICs, GPUs). `powersave`/`powersupersave` should reduce
idle power with little load impact; the risk is a small latency add on link wake. Mostly
an idle-power optimization.

## Implementation
- Playbook: `ansible/optimizations/pcie_aspm.yml`, var `pcie_aspm_policy`.
- Apply: `ansible-playbook -i ansible/hosts ansible/optimizations/pcie_aspm.yml -e pcie_aspm_policy=powersave`
- Revert: `reset_to_baseline.yml` (policy=default)
- Verify: `cat /sys/module/pcie_aspm/parameters/policy` (active policy in brackets).
- Do not use `pcie_aspm=force`; its measured idle result was worse than baseline.

## Measurement

Phase C values are means +/- sample stdev over N=5 valid repeats. Load coverage was
at least 0.98 and every included run had zero dropped packets.

| Metric | Baseline | powersave | Delta |
|--------|---------:|----------:|------:|
| Idle power | 4.355 +/- 0.043 W | 4.091 +/- 0.155 W | -6.06% |
| Compile score | 135.590 +/- 0.344 s | 135.849 +/- 0.384 s | +0.19% slower |
| Energy-to-complete | 3.301 +/- 0.012 Wh | 3.318 +/- 0.010 Wh | +0.52% |
| Avg load power | 79.004 W | 79.496 W | +0.62% |
| Peak load power | 96.700 W | 96.600 W | -0.10% |

The earlier N=3 screen measured `powersupersave` at 4.367 +/- 0.042 W idle,
136.365 +/- 0.591 s, and 3.325 +/- 0.024 Wh. It was not promoted.

## Analysis
`powersave` is a confirmed idle-only improvement and stays comfortably within the
142.726 s performance floor. A two-sided Welch test supports the idle difference
(`p=0.0167`) and finds no compile-time difference (`p=0.2949`). It does not reduce
fixed-work compile energy: the small 0.52% regression is statistically detectable in
these repeats (`p=0.0425`). It also misses the project's 15% idle target. The higher
tuned idle variance must be retained in any report rather than presenting only the
0.264 W mean reduction.

Residency diagnosis reproduces the mechanism. Baseline is about 96-97% package PC2
at roughly 2.2-2.35 W package power. Runtime `powersave` shifts the package to about
93-95% PC6 at roughly 1.35-1.47 W, but still reaches neither PC8 nor PC10.

The root NVMe path already has L1.2 enabled end to end. Temporarily allowing runtime
PM on its PCI endpoint left it active (`runtime_usage=1`) and did not change the
PC6-only result. Thunderbolt, USB, GPU, audio, and NPU devices were already suspended
or idle. The leading remaining hypothesis is the active I226 path: it is a 2.5 Gb/s
link with EEE disabled, its partner advertises no EEE, and root port `0000:00:1c.5`
exposes only L1.1. This is an inference, not a confirmed cause; testing it requires
console/out-of-band access or moving management traffic to another interface.

## Verdict
- [x] Keep   - [ ] Revert   - [ ] Refine: keep runtime `powersave` only as a
  conservative idle profile (`ansible/profiles/conservative_idle.yml`). Do not claim
  a load-energy win or a 15% idle win.
