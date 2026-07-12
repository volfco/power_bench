# Network (NIC) power

## Hypothesis
Energy Efficient Ethernet (EEE) lets the PHY idle between frames, Wake-on-LAN keeps
extra logic powered, and Wi-Fi power save idles the radio. These are primarily idle
optimizations, but EEE requires support at both ends of the link.

## Implementation
- Playbook: `ansible/optimizations/network_power.yml`, vars `nic_power_save`, `nic_interface`.
- Apply: `ansible-playbook -i ansible/hosts ansible/optimizations/network_power.yml -e nic_power_save=true`
- Revert explicitly with `nic_power_save=false`.
- Verify: `ethtool --show-eee <if>` and the Wake-on-LAN line from `ethtool <if>`.

## Measurement

The generic `nic_power_save` screen stopped after N=2 valid idle runs because its
4.397 +/- 0.040 W result was 1.20% worse than the contemporaneous 4.345 W baseline.

## Analysis
The active SSH route is `enp172s0` on I226 `0000:ac:00.0`, linked at 2.5 Gb/s. EEE is
disabled and its link partner advertises no EEE modes, so enabling EEE locally is not
a viable standalone test. Root port `0000:00:1c.5` and the endpoint currently use
ASPM L1.1; the root port reports no L1.2 capability even though the I226 endpoint has
it. This is the leading remaining PC8-blocker hypothesis after USB4, GPU, audio, NPU,
and NVMe checks, but it is not yet causally confirmed.

Do not alter, unbind, down, renegotiate, or runtime-suspend this interface over the
only SSH path. A meaningful comparison needs console/out-of-band access or an
alternate management route, then tests of link down, an EEE-capable partner, and
possibly 1 Gb/s link speed in that order.

`ansible/preflight_nic_isolation.yml` is the mandatory read-only gate for those
experiments. It derives the controller and target addresses from the live SSH session
and refuses to proceed if either the return route or SSH target address uses the NIC
under test. The inactive I226 currently has no cable/carrier. The Wi-Fi adapter is
healthy and a temporary active scan found nearby 6 GHz networks, but the host has no
saved Wi-Fi credentials or configured Wi-Fi netplan entry. Therefore neither interface
is currently a verified alternate management path.

Once that preflight passes for `enp172s0`, use
`ansible/diagnose_nic_residency.yml` for the first meter-free test. It takes paired
20-second `turbostat` samples with ASPM `powersave`, lowers only the isolated NIC for
the second sample, rechecks the controller route immediately before link-down, and
restores both NIC administrative state and the original ASPM policy in an `always`
block. Do not add meter repeats unless this paired probe unlocks PC8 or causes another
clear package-residency change.

The harness's full path was exercised with two-second samples against isolated,
no-carrier `enp171s0`. Both turbostat samples completed, the second safety check
passed, and the `always` block restored the interface's administrative-up state and
the original deployed ASPM `powersave` policy. This was a harness validation only,
not evidence about the active I226 hypothesis.

## Verdict
- [ ] Keep   - [ ] Revert   - [x] Refine: reject the generic screen; isolate the
  active I226 path only after an alternate management path exists.
