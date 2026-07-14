#!/usr/bin/env python3
"""OFAT power-optimization sweep driver.

Walks every optimization in the catalog (one factor at a time, from baseline) against the
test suite. Each iteration:

  1. writes a minimal Ansible varfile with ONLY the non-default knobs for the variant
     (ansible/vars/iter_<NNNN>_<label>.yml),
  2. applies it with apply_optimizations.yml (which verifies every knob took effect),
  3. runs run_benchmark.py (logs power, stores the result, then reboots the host),

so every iteration begins on a freshly booted baseline — the reboot is the reset. If an
apply or benchmark fails, the host is rebooted before the next iteration so a failed run
can never leak its knobs, and the sweep ends with one reconcile-to-defaults apply so a
trailing kernel_params variant can't leave GRUB dirty.

Each variant is tagged with the objective it targets: 'load'/'both' variants run the full
--tests list; 'idle' variants run a dedicated --idle-only measurement plus one quick load
test (the defconfig-only kernel build suite) for the performance floor. All repeats are
counted — there is no warm-up discard (every run starts freshly booted; the thermal gate
replaces it).

The host must be in the Ansible inventory (ansible/hosts) AND reachable by run_benchmark
over SSH at the same address. Start from a freshly booted host (or pass --initial-reboot).

Examples:
  python run_suite.py 192.168.1.58 --user metrolla --mac 45:AF:4E:55:56:06 \
      --checksum-policy warn --cool-to 55                         # current rig
  python run_suite.py 192.168.1.58 --user metrolla --only cpu_governor sched_ext
  python run_suite.py 192.168.1.58 --inventory inventory.yml --ansible-limit node-a
  python run_suite.py 192.168.1.58 --list                         # print the matrix and exit
  python run_suite.py 192.168.1.58 --user metrolla --dry-run       # show commands, run nothing
  python run_suite.py 192.168.1.58 --user metrolla --shuffle --seed 1
  python run_suite.py 192.168.1.76 --sweep amd --only 'stack=amd_performance+pcie_aspm' --dry-run
"""

import argparse
import hashlib
import json
import logging
import os
import subprocess
import sys

# Fresh-boot defaults — mirror of ansible/vars/defaults.yml. Used to strip default-valued
# keys so each varfile carries only genuine overrides. HOST-DEPENDENT entries (governor,
# io_scheduler) are None here so an explicit choice is never mistaken for the default.
DEFAULTS = {
    "cpu_governor": None,            # host-dependent
    "turbo_enabled": True,
    "cstate_limit": -1,
    "energy_perf_preference": "default",
    "pstate_max_perf_pct": 100,
    "pstate_min_perf_pct": 0,
    "pcie_aspm_policy": "default",
    "io_scheduler": None,            # host-dependent
    "usb_autosuspend": False,
    "sata_link_pm": "max_performance",
    "hdd_apm_level": 254,
    "gpu_power_profile": "auto",
    "services_to_disable": [],
    "nic_power_save": False,
    "scx_scheduler": "none",
    "scx_flags": "",
    "kernel_params": [],
}

# Pseudo-test label for dedicated --idle-only measurements, and the quick load test that
# gives idle-targeted knobs their performance floor. The local suite pins PTS to only the
# defconfig build option; the raw pts/build-linux-kernel profile also runs allmodconfig.
IDLE_TEST = "idle"
BUILD_KERNEL_DEFCONFIG_TEST = "local/power-bench-build-kernel-defconfig-1.0.0"
PERF_FLOOR_TEST = BUILD_KERNEL_DEFCONFIG_TEST

# The OFAT catalog: (label, {non-default knobs}, target). Every run starts freshly booted
# (= baseline), so the delta is attributable to that one change. target selects the tests
# (see plan.md "Match tests to the knob's target column"):
#   'load' / 'both' -> the full --tests list (idle screened from their gated idle windows)
#   'idle'          -> a dedicated --idle-only run + defconfig kernel build (perf floor)
# 'baseline' additionally gets the idle-only reference run.
# For Phase B (stacking winners) add variants whose dict combines several knobs.
EXPERIMENTS = [
    # reference
    ("baseline", {}, "both"),

    # 2. CPU governor
    ("cpu_governor=powersave",   {"cpu_governor": "powersave"}, "both"),
    ("cpu_governor=schedutil",   {"cpu_governor": "schedutil"}, "both"),
    ("cpu_governor=performance", {"cpu_governor": "performance"}, "both"),   # perf-ceiling reference

    # 3. Turbo boost
    ("turbo=off", {"turbo_enabled": False}, "load"),

    # 4. C-states (DIAGNOSTIC: limiting RAISES idle power; baseline already enables all)
    ("cstates=shallow", {"cstate_limit": 1}, "idle"),

    # 5. intel_pstate / HWP
    ("epp=performance",   {"energy_perf_preference": "performance"}, "both"),
    ("epp=power",         {"energy_perf_preference": "power"}, "both"),
    ("epp=balance_power", {"energy_perf_preference": "balance_power"}, "both"),
    ("max_perf_pct=90",   {"pstate_max_perf_pct": 90}, "both"),
    ("max_perf_pct=80",   {"pstate_max_perf_pct": 80}, "both"),
    ("max_perf_pct=70",   {"pstate_max_perf_pct": 70}, "both"),

    # Phase B: combine the independently confirmed load and idle winners.
    ("stack=balanced_load+conservative_idle", {
        "pstate_max_perf_pct": 90,
        "pcie_aspm_policy": "powersave",
    }, "both"),

    # 6. PCIe ASPM
    ("pcie_aspm=powersave",      {"pcie_aspm_policy": "powersave"}, "idle"),
    ("pcie_aspm=powersupersave", {"pcie_aspm_policy": "powersupersave"}, "idle"),

    # 7. I/O scheduler
    ("io=none",  {"io_scheduler": "none"}, "load"),
    ("io=mq-deadline", {"io_scheduler": "mq-deadline"}, "load"),
    ("io=bfq",   {"io_scheduler": "bfq"}, "load"),
    ("io=kyber", {"io_scheduler": "kyber"}, "load"),

    # 8. USB autosuspend
    ("usb_autosuspend", {"usb_autosuspend": True}, "idle"),

    # 9. Disk power
    ("sata=med_dipm",  {"sata_link_pm": "med_power_with_dipm"}, "idle"),
    ("sata=min_power", {"sata_link_pm": "min_power"}, "idle"),

    # 10. GPU power
    ("gpu=low", {"gpu_power_profile": "low"}, "idle"),

    # 11. Service trimming (EDIT this list to match the host's installed services)
    ("services=trim", {"services_to_disable": [
        "bluetooth.service", "cups.service", "avahi-daemon.service", "ModemManager.service"]},
     "idle"),

    # 12. NIC power
    ("nic_power_save", {"nic_power_save": True}, "idle"),

    # 13. sched_ext (SCX) schedulers
    ("sched_ext=scx_lavd:powersave",    {"scx_scheduler": "scx_lavd",    "scx_flags": "--powersave"}, "load"),
    ("sched_ext=scx_lavd:performance",  {"scx_scheduler": "scx_lavd",    "scx_flags": "--performance"}, "load"),
    ("sched_ext=scx_bpfland:powersave", {"scx_scheduler": "scx_bpfland", "scx_flags": "-m powersave"}, "load"),
    ("sched_ext=scx_rusty",             {"scx_scheduler": "scx_rusty"}, "load"),
    ("sched_ext=scx_flash",             {"scx_scheduler": "scx_flash"}, "load"),
    # scx_tickless v1.1.1 is incompatible with this 7.0.0-27 generic kernel:
    # it ejects at attach with "starting timer on cpu8, which is not a scheduling
    # CPU". Keep it out of unattended sweeps until an upstream-compatible release
    # is deliberately audited on the target (the binary remains installed for that).

    # 14. Kernel boot params (REBOOT to apply/clear; apply_optimizations reconciles GRUB)
    ("kernel_params=pcie_aspm_force",      {"kernel_params": ["pcie_aspm=force"]}, "idle"),
    ("kernel_params=intel_pstate_passive", {"kernel_params": ["intel_pstate=passive"]}, "both"),
    # Security-sensitive diagnostic cases. These are never part of the default core
    # selection; run only on an isolated benchmark host with an explicit --only value.
    ("kernel_params=mitigations_off", {"kernel_params": ["mitigations=off"]}, "both"),
    ("kernel_params=nokaslr", {"kernel_params": ["nokaslr"]}, "both"),
    ("kernel_params=mitigations_off+nokaslr", {
        "kernel_params": ["mitigations=off", "nokaslr"],
    }, "both"),
]

# AMD/acpi-cpufreq catalog. Keep this separate from EXPERIMENTS: the Intel catalog
# contains HWP/EPP, intel_pstate max_perf_pct, and an Intel-only stack that must never
# be selected for node2. The AMD host inventory advertises this profile explicitly.
AMD_EXPERIMENTS = [
    ("baseline", {}, "both"),

    # All governors reported by node2's acpi-cpufreq driver. The userspace case is
    # intentionally a governor-only probe; no fixed frequency is imposed by the suite.
    ("cpu_governor=conservative", {"cpu_governor": "conservative"}, "both"),
    ("cpu_governor=ondemand",     {"cpu_governor": "ondemand"}, "both"),
    ("cpu_governor=userspace",    {"cpu_governor": "userspace"}, "both"),
    ("cpu_governor=powersave",    {"cpu_governor": "powersave"}, "both"),
    ("cpu_governor=performance",  {"cpu_governor": "performance"}, "both"),
    ("cpu_governor=schedutil",    {"cpu_governor": "schedutil"}, "both"),

    # Supported acpi-cpufreq/AMD platform controls.
    ("turbo=off", {"turbo_enabled": False}, "load"),
    ("cstates=shallow", {"cstate_limit": 1}, "idle"),
    ("pcie_aspm=powersave", {"pcie_aspm_policy": "powersave"}, "idle"),
    ("pcie_aspm=powersupersave", {"pcie_aspm_policy": "powersupersave"}, "idle"),
    ("gpu=low", {"gpu_power_profile": "low"}, "idle"),

    # AMD equivalent of the old combined experiment: use the existing valid
    # performance governor reference plus the supported generic ASPM control.
    ("stack=amd_performance+pcie_aspm", {
        "cpu_governor": "performance",
        "pcie_aspm_policy": "powersave",
    }, "both"),

    # Architecture-independent, security-sensitive boot-parameter diagnostic cases.
    # They remain opt-in through --only and are reconciled back to an empty managed
    # GRUB fragment at the end of every non-dry sweep.
    ("kernel_params=mitigations_off", {"kernel_params": ["mitigations=off"]}, "both"),
    ("kernel_params=nokaslr", {"kernel_params": ["nokaslr"]}, "both"),
    ("kernel_params=mitigations_off+nokaslr", {
        "kernel_params": ["mitigations=off", "nokaslr"],
    }, "both"),
]

SWEEP_EXPERIMENTS = {
    "core": EXPERIMENTS,
    "amd": AMD_EXPERIMENTS,
}


def yaml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(yaml_value(x) for x in v) + "]"
    s = str(v)
    if s and all(c.isalnum() or c in "._-=/" for c in s):
        return s
    return '"%s"' % s.replace('"', '\\"')


def nondefaults(overrides):
    """Return only the keys whose value differs from the fresh-boot default."""
    return {k: v for k, v in overrides.items() if DEFAULTS.get(k, object()) != v}


def config_hash(nd):
    """Short stable hash of the non-default knobs, stored with every run."""
    return hashlib.sha256(json.dumps(nd, sort_keys=True, default=str).encode()).hexdigest()[:12]


def write_varfile(path, overrides):
    nd = nondefaults(overrides)
    with open(path, "w") as f:
        f.write("# Generated by run_suite.py — non-default knobs only.\n")
        if not nd:
            f.write("{}\n")   # ansible rejects an -e @file with no YAML data
        for k, v in nd.items():
            f.write(f"{k}: {yaml_value(v)}\n")
    return nd


def safe_name(label):
    return label.replace("=", "_").replace(":", "_").replace("/", "_").replace(" ", "")


def sh(cmd):
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd).returncode


def select_experiments(only, skip_baseline, sweep="core", exclude=None):
    """Select variants from one named hardware profile."""
    selected = list(SWEEP_EXPERIMENTS[sweep])
    if only:
        selected = [e for e in selected
                    if e[0] == "baseline" or any(p in e[0] for p in only)]
    if exclude:
        selected = [e for e in selected
                    if not any(p in e[0] for p in exclude)]
    if skip_baseline:
        selected = [e for e in selected if e[0] != "baseline"]
    return selected


def print_matrix(selected):
    print(f"{'#':>3}  {'reboot':^6}  {'target':^6}  {'variant':<34}  non-default knobs")
    print("-" * 98)
    for i, (label, overrides, target) in enumerate(selected, 1):
        reboots = "yes" if "kernel_params" in overrides else ""
        nd = nondefaults(overrides)
        nd_s = ", ".join(f"{k}={v}" for k, v in nd.items()) or "(none — baseline)"
        print(f"{i:>3}  {reboots:^6}  {target:^6}  {label:<34}  {nd_s}")


def variant_tests(label, target, tests):
    """Which measurements a variant gets (plan.md 'Match tests to the knob's target')."""
    if target == "idle":
        return [IDLE_TEST, PERF_FLOOR_TEST]
    if label == "baseline":
        return [IDLE_TEST] + list(tests)   # baseline also anchors the idle-only reference
    return list(tests)


def build_jobs(selected, tests, repeats):
    return [(label, overrides, test, r)
            for label, overrides, target in selected
            for test in variant_tests(label, target, tests)
            for r in range(1, repeats + 1)]


def existing_valid_jobs(db_path, jobs, idle_duration, host):
    """Return jobs that already have a valid completed row in the DB.

    The suite is long and target reboots are noisy. This lets a stopped sweep resume
    without duplicating good measurements, while still re-running missing or invalid
    rows. Runs are scoped to ``host`` so a shared DuckDB can safely hold measurements
    for multiple nodes. Validity mirrors the handoff criteria: zero dropped packets,
    good idle sample count for idle-only runs, and coverage >= 0.9 with a stored score
    for load runs.
    """
    try:
        import duckdb
    except ImportError:
        logging.warning("--skip-existing requested but duckdb is unavailable")
        return set()

    try:
        con = duckdb.connect(db_path, read_only=True)
    except Exception as exc:
        logging.warning("--skip-existing could not read %s: %s", db_path, exc)
        return set()

    completed = set()
    idle_min_samples = int(idle_duration * 0.9)
    for label, overrides, test, repeat in jobs:
        cfg = config_hash(nondefaults(overrides))
        if test == IDLE_TEST:
            row = con.execute(
                """
                SELECT r.run_id, COUNT(rd.*) FILTER (WHERE rd.phase = 'idle') AS idle_samples
                FROM runs r
                LEFT JOIN readings rd ON rd.run_id = r.run_id
                WHERE r.host = ? AND r.optimization = ? AND r.test = ? AND r.repeat_idx = ?
                  AND r.config_hash = ? AND COALESCE(r.dropped_packets, 0) = 0
                GROUP BY r.run_id
                HAVING idle_samples >= ?
                LIMIT 1
                """,
                [host, label, test, repeat, cfg, idle_min_samples],
            ).fetchone()
        else:
            row = con.execute(
                """
                SELECT run_id
                FROM runs
                WHERE host = ? AND optimization = ? AND test = ? AND repeat_idx = ?
                  AND config_hash = ? AND COALESCE(dropped_packets, 0) = 0
                  AND COALESCE(bench_sample_coverage, 0) >= 0.9
                  AND bench_score IS NOT NULL
                LIMIT 1
                """,
                [host, label, test, repeat, cfg],
            ).fetchone()
        if row:
            completed.add((label, test, repeat, cfg))
    con.close()
    return completed


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", help="SSH target; must also resolve to the inventory host")
    ap.add_argument("--user", "-u")
    ap.add_argument("--key", "-i")
    ap.add_argument("--mac", "-m", default=None, help="BLE MAC of the Atorch meter")
    ap.add_argument("--db", default="benchmarks/power_meter.duckdb")
    ap.add_argument("--tests", nargs="+", default=[BUILD_KERNEL_DEFCONFIG_TEST])
    ap.add_argument("--repeats", type=int, default=3,
                    help="runs per (variant, test); all counted, no warm-up (default: 3)")
    ap.add_argument("--inventory", default="ansible/hosts")
    ap.add_argument("--sweep", choices=sorted(SWEEP_EXPERIMENTS), default="core",
                    help="hardware-aware catalog to run (default: core; use amd for node2)")
    ap.add_argument("--ansible-limit", default=None,
                    help="limit each nested apply_optimizations play to this inventory host/pattern; "
                         "required when the inventory contains more than one benchmark node")
    ap.add_argument("--vars-dir", default="ansible/vars")
    ap.add_argument("--settle", type=float, default=30.0)
    ap.add_argument("--idle-duration", type=float, default=600.0,
                    help="stable-idle window length for --idle-only runs (default: 600 s)")
    ap.add_argument("--cool-to", type=float, default=None,
                    help="thermal gate: wait until host CPU temp is at/below this (C) "
                         "before each bench phase (default: record-only)")
    ap.add_argument("--checksum-policy", choices=["strict", "warn"], default="strict",
                    help="meter checksum handling passed to run_benchmark.py")
    ap.add_argument("--only", nargs="+", metavar="PATTERN",
                    help="run only variants whose label contains any of these substrings "
                         "(baseline is always included unless --skip-baseline)")
    ap.add_argument("--exclude", nargs="+", metavar="PATTERN",
                    help="skip variants whose label contains any of these substrings")
    ap.add_argument("--skip-baseline", action="store_true")
    ap.add_argument("--list", action="store_true", help="print the variant matrix and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the apply/benchmark commands without running anything")
    ap.add_argument("--skip-existing", action="store_true",
                    help="resume mode: skip jobs that already have a valid DB row "
                         "for the same variant, test, repeat, and config hash")
    ap.add_argument("--shuffle", action="store_true",
                    help="randomize iteration order to spread out drift across the session")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed for --shuffle (reproducible)")
    ap.add_argument("--initial-reboot", action="store_true",
                    help="reboot the host once before the first iteration")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-5s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    selected = select_experiments(args.only, args.skip_baseline, args.sweep, args.exclude)
    if not selected:
        print("No variants selected.", file=sys.stderr)
        sys.exit(1)

    if args.list:
        print_matrix(selected)
        return

    jobs = build_jobs(selected, args.tests, args.repeats)
    requested_jobs = list(jobs)
    skipped_existing = 0
    if args.skip_existing:
        completed = existing_valid_jobs(args.db, jobs, args.idle_duration, args.host)
        before = len(jobs)
        jobs = [
            job for job in jobs
            if (job[0], job[2], job[3],
                config_hash(nondefaults(job[1]))) not in completed
        ]
        skipped_existing = before - len(jobs)
    if args.shuffle:
        import random
        random.Random(args.seed).shuffle(jobs)

    print(f"Sweep: {len(selected)} variants, {args.repeats} repeats each = {len(jobs)} runs"
          f"{' (shuffled)' if args.shuffle else ''}.")
    if skipped_existing:
        print(f"Skipped {skipped_existing} existing valid run(s).")
    print(f"Tests: {', '.join(args.tests)} (idle-targeted variants: {IDLE_TEST} + {PERF_FLOOR_TEST})")
    if args.dry_run:
        print("(dry run — no varfiles written, nothing applied or measured)\n")

    reboot_host = None
    if not args.dry_run:
        from run_benchmark import reboot_host   # reuse the reboot-and-wait helper
        os.makedirs(args.vars_dir, exist_ok=True)
        if args.initial_reboot:
            reboot_host(args.host, args.user, args.key)

    for i, (label, overrides, test, r) in enumerate(jobs, 1):
        vpath = os.path.join(args.vars_dir, f"iter_{i:04d}_{safe_name(label)}.yml")
        nd = nondefaults(overrides)
        print(f"\n=== iter {i}/{len(jobs)}: {label} | test={test} | repeat={r} | nondefaults={nd} ===",
              flush=True)

        apply_cmd = ["ansible-playbook", "-i", args.inventory]
        if args.ansible_limit:
            apply_cmd += ["--limit", args.ansible_limit]
        apply_cmd += ["ansible/apply_optimizations.yml", "-e", f"@{vpath}"]
        bench_cmd = [sys.executable, "run_benchmark.py", args.host]
        if test != IDLE_TEST:
            bench_cmd.append(test)
        bench_cmd += ["--db", args.db, "--optimization", label,
                      "--repeat", str(r), "--settle", str(args.settle),
                      "--config-hash", config_hash(nd), "--reboot"]
        if test == IDLE_TEST:
            bench_cmd += ["--idle-only", "--idle-duration", str(args.idle_duration)]
        elif args.cool_to is not None:
            bench_cmd += ["--cool-to", str(args.cool_to)]
        if args.user:
            bench_cmd += ["--user", args.user]
        if args.mac:
            bench_cmd += ["--mac", args.mac]
        if args.checksum_policy != "strict":
            bench_cmd += ["--checksum-policy", args.checksum_policy]
        if args.key:
            bench_cmd += ["--key", args.key]

        if args.dry_run:
            print("  would write " + vpath + ":  " + (str(nd) if nd else "(empty)"))
            print("  + " + " ".join(apply_cmd))
            print("  + " + " ".join(bench_cmd))
            continue

        write_varfile(vpath, overrides)
        if sh(apply_cmd) != 0:
            print(f"!! apply failed for iter {i}; rebooting to clear partial state, then skipping",
                  flush=True)
            reboot_host(args.host, args.user, args.key)
            continue
        if sh(bench_cmd) != 0:
            # run_benchmark reboots on its own failure paths, but belt-and-braces: a
            # crashed process must never leak this iteration's knobs into the next.
            print(f"!! benchmark failed for iter {i}; rebooting before the next iteration",
                  flush=True)
            reboot_host(args.host, args.user, args.key)

    if not args.dry_run:
        # Reconcile GRUB back to defaults so a trailing kernel_params variant can't
        # leave a persistent boot param behind.
        final = os.path.join(args.vars_dir, "iter_final_reconcile.yml")
        with open(final, "w") as f:
            f.write("# Final reconcile — everything back to fresh-boot defaults.\n"
                    "kernel_params: []\n")
        print("\nFinal reconcile to defaults...", flush=True)
        final_cmd = ["ansible-playbook", "-i", args.inventory]
        if args.ansible_limit:
            final_cmd += ["--limit", args.ansible_limit]
        final_cmd += ["ansible/apply_optimizations.yml", "-e", f"@{final}"]
        final_rc = sh(final_cmd)

        # A child command can fail after creating a partial row (or a meter can
        # produce an invalid row while the benchmark itself exits 0).  Do not
        # report a sweep as successful until every requested repeat has a valid
        # row for this host.  This also makes --skip-existing a safe resume mode.
        completed = existing_valid_jobs(args.db, requested_jobs,
                                        args.idle_duration, args.host)
        missing = [
            (label, test, repeat)
            for label, overrides, test, repeat in requested_jobs
            if (label, test, repeat,
                config_hash(nondefaults(overrides))) not in completed
        ]
        if final_rc != 0 or missing:
            if final_rc != 0:
                print("!! final reconcile failed", file=sys.stderr)
            if missing:
                print("!! sweep has no valid result for:", file=sys.stderr)
                for label, test, repeat in missing:
                    print(f"   {label} | test={test} | repeat={repeat}", file=sys.stderr)
            sys.exit(1)

    print(f"\nSweep complete: {len(jobs)} iterations.", flush=True)


if __name__ == "__main__":
    main()
