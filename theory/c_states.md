# C-states (CPU idle)

## Hypothesis
Allowing deeper C-states lets idle cores power down further, reducing **idle power**;
the cost is wake latency. This is primarily an idle-power optimization.

## Implementation
- Playbook: `ansible/optimizations/c_states.yml`, var `cstate_limit` (-1 = enable all).
- Apply (enable all): `ansible-playbook -i ansible/hosts ansible/optimizations/c_states.yml`
- Revert: `reset_to_baseline.yml` (enables all)
- Verify: per-state `disable` flags reported by the playbook.

## Measurement

No meter sweep was needed because the baseline already enables every exposed state:
`POLL C1E C6 C10`. A controlled baseline `turbostat` sample showed about 99.8% CPU
C10 residency while package residency remained about 96-97% PC2 and zero PC6/PC8.
With PCIe ASPM `powersave`, CPU C10 remained about 99.8% while the package moved to
about 93-95% PC6.

## Analysis
Core idle is not the limiting layer. All deep core states are available and heavily
used; package residency changes independently with PCIe policy. Limiting C-states
would move in the wrong direction, and re-applying the already-enabled C10 state is a
no-op.

## Verdict
- [ ] Keep   - [x] Revert   - [ ] Refine: retain the baseline all-states-enabled
  configuration and do not spend meter time on a C-state limit sweep.
