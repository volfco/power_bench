#!/usr/bin/env python3
"""Run one pass over pending benchmarks for one Ansible inventory host.

Persisted attempts are capped at four per host, variant, test, and configuration.

Examples:
  python run_pending_benchmarks.py node2 --mac AA:BB:CC:DD:EE:FF
  python run_pending_benchmarks.py node2 --dry-run
  python run_pending_benchmarks.py node2 --only pcie_aspm --skip-baseline
"""

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from ansible_inventory import InventoryHostError, resolve_inventory_host


ROOT = Path(__file__).resolve().parent
DEFAULT_INVENTORY = ROOT / "ansible" / "hosts"


def option_value(arguments: list[str], option: str, default: str) -> str:
    """Read an option's value from pass-through arguments without parsing them."""
    for index, argument in enumerate(arguments):
        if argument == option and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith(f"{option}="):
            return argument.split("=", 1)[1]
    return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "host",
        help="Ansible inventory host name (literal IP addresses are rejected)",
    )
    args, suite_args = parser.parse_known_args(argv)

    prohibited = (
        "--repeats",
        "--skip-existing",
        "--no-skip-existing",
        "--run-cap",
    )
    if any(
        argument == option or argument.startswith(f"{option}=")
        for argument in suite_args
        for option in prohibited
    ):
        parser.error(
            "this command always uses --repeats 1 --skip-existing --run-cap 4"
        )

    inventory = option_value(suite_args, "--inventory", str(DEFAULT_INVENTORY))
    try:
        resolve_inventory_host(args.host, inventory)
    except InventoryHostError as exc:
        parser.error(str(exc))

    command = [
        sys.executable,
        str(ROOT / "run_suite.py"),
        args.host,
        "--repeats",
        "3",
        "--skip-existing",
        "--run-cap",
        "3",
        *suite_args,
    ]
    print("+ " + shlex.join(command), flush=True)
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
