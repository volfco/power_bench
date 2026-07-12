# Service trimming

## Hypothesis
Background services can cause periodic CPU wakeups and prevent deep package states.
Stopping unneeded services should therefore reduce idle power when wakeups are a real
limiting factor.

## Implementation
- Playbook: `ansible/optimizations/services.yml`, vars `services_to_disable`, `services_to_enable`.
- The standalone playbook persists changes; revert explicitly with `services_to_enable`.
- The sweep path stops services only for the current boot.

## Measurement

The targeted idle screen stopped after N=2 valid runs. Service trimming measured
4.398 +/- 0.058 W versus the contemporaneous 4.345 W baseline, a 1.22% regression.
Both runs had zero dropped packets and at least 590 stable idle samples.

## Analysis
The result is within ordinary idle variation and is in the wrong direction. The later
residency pass also showed roughly 99.8% CPU C10, so periodic service wakeups are not
the package-idle blocker on this host.

## Verdict
- [ ] Keep   - [x] Revert   - [ ] Refine: do not disable services for power reasons.
