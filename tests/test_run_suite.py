import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import run_suite


class AmdSweepTests(unittest.TestCase):
    def test_core_profile_remains_the_default_catalog(self):
        self.assertEqual(
            run_suite.select_experiments(None, False), run_suite.EXPERIMENTS)

    def test_exclude_removes_matching_variants(self):
        selected = run_suite.select_experiments(
            None, False, exclude=["sched_ext"])
        labels = [label for label, _, _ in selected]
        self.assertIn("baseline", labels)
        self.assertFalse(any("sched_ext" in label for label in labels))

    def test_amd_catalog_contains_supported_controls_only(self):
        labels = [label for label, _, _ in run_suite.AMD_EXPERIMENTS]
        self.assertIn("cpu_governor=conservative", labels)
        self.assertIn("cpu_governor=ondemand", labels)
        self.assertIn("cpu_governor=userspace", labels)
        self.assertIn("cpu_governor=powersave", labels)
        self.assertIn("cpu_governor=performance", labels)
        self.assertIn("cpu_governor=schedutil", labels)
        self.assertIn("stack=amd_performance+pcie_aspm", labels)
        self.assertFalse(any("epp" in label or "max_perf_pct" in label
                             or label.startswith("stack=balanced_")
                             for label in labels))
        for _, overrides, _ in run_suite.AMD_EXPERIMENTS:
            self.assertNotIn("energy_perf_preference", overrides)
            self.assertNotIn("pstate_max_perf_pct", overrides)
            self.assertNotIn("pstate_min_perf_pct", overrides)

    def test_amd_combined_branch_is_explicit_and_non_intel(self):
        selected = run_suite.select_experiments(
            ["stack=amd_performance+pcie_aspm"], False, sweep="amd")
        labels = [label for label, _, _ in selected]
        self.assertEqual(labels, ["baseline", "stack=amd_performance+pcie_aspm"])
        combined = selected[-1][1]
        self.assertEqual(combined, {
            "cpu_governor": "performance",
            "pcie_aspm_policy": "powersave",
        })

    def test_amd_combined_branch_dry_run(self):
        result = subprocess.run(
            [
                sys.executable, "run_suite.py", "node2",
                "--sweep", "amd",
                "--only", "stack=amd_performance+pcie_aspm",
                "--skip-baseline", "--repeats", "1", "--dry-run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stack=amd_performance+pcie_aspm", result.stdout)
        self.assertIn("'cpu_governor': 'performance'", result.stdout)
        self.assertIn("'pcie_aspm_policy': 'powersave'", result.stdout)
        self.assertNotIn("max_perf_pct", result.stdout)
        self.assertNotIn("energy_perf_preference", result.stdout)

    def test_security_sensitive_kernel_param_variants_are_opt_in_for_every_sweep(self):
        expected = {
            "kernel_params=mitigations_off": ["mitigations=off"],
            "kernel_params=nokaslr": ["nokaslr"],
            "kernel_params=mitigations_off+nokaslr": ["mitigations=off", "nokaslr"],
        }
        for sweep in ("core", "amd"):
            variants = {label: overrides for label, overrides, _ in run_suite.SWEEP_EXPERIMENTS[sweep]}
            for label, kernel_params in expected.items():
                self.assertEqual(variants[label]["kernel_params"], kernel_params)

    def test_new_single_variable_variants_are_available(self):
        core = {label: overrides for label, overrides, _ in run_suite.EXPERIMENTS}
        self.assertEqual(core["max_perf_pct=95"], {"pstate_max_perf_pct": 95})

        portable_kernel_variants = {
            "kernel_params=nosmt": ["nosmt"],
            "kernel_params=nmi_watchdog_0": ["nmi_watchdog=0"],
        }
        for sweep in ("core", "amd"):
            variants = {
                label: overrides
                for label, overrides, _ in run_suite.SWEEP_EXPERIMENTS[sweep]
            }
            for label, kernel_params in portable_kernel_variants.items():
                self.assertEqual(variants[label], {"kernel_params": kernel_params})

    def test_combined_kernel_param_variant_dry_run(self):
        result = subprocess.run(
            [
                sys.executable, "run_suite.py", "node2",
                "--sweep", "amd",
                "--only", "kernel_params=mitigations_off+nokaslr",
                "--skip-baseline", "--repeats", "1", "--dry-run",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kernel_params=mitigations_off+nokaslr", result.stdout)
        self.assertIn("'kernel_params': ['mitigations=off', 'nokaslr']", result.stdout)

    def test_build_jobs_expands_multiple_tests(self):
        selected = [("variant", {}, "load")]

        jobs = run_suite.build_jobs(selected, ["test/one", "test/two"], repeats=2)

        self.assertEqual(
            [(test, repeat) for _, _, test, repeat in jobs],
            [("test/one", 1), ("test/one", 2), ("test/two", 1), ("test/two", 2)],
        )

    def test_literal_ip_host_is_rejected(self):
        result = subprocess.run(
            [sys.executable, "run_suite.py", "192.168.1.76", "--list"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must be an Ansible inventory name", result.stderr)


if __name__ == "__main__":
    unittest.main()
