#!/usr/bin/env python3
"""Run every missing valid benchmark repeat for one host.

This is the resume entry point for the normal three-repeat matrix.  It delegates
to ``run_suite.py``, whose database query treats an existing row as complete only
when it meets the power-data validity criteria.  Thus a failed or incomplete row
is retried, while valid repeat indexes 1 through 3 are left alone.

Examples:
  python run_pending_benchmarks.py 192.168.1.76 --user metrolla --mac AA:BB:CC:DD:EE:FF
  python run_pending_benchmarks.py 192.168.1.76 --dry-run
  python run_pending_benchmarks.py 192.168.1.76 --only pcie_aspm --skip-baseline
"""

import argparse
import shlex
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_INVENTORY = ROOT / "ansible" / "hosts"


def inventory_sweep(host: str, inventory: Path) -> str:
    """Return the configured sweep profile for ``host``, or the core profile."""
    try:
        lines = inventory.read_text().splitlines()
    except OSError:
        return "core"

    for line in lines:
        fields = line.split()
        if not fields or fields[0].startswith(("#", "[")):
            continue
        values = dict(field.split("=", 1) for field in fields[1:] if "=" in field)
        if host not in (fields[0], values.get("ansible_host")):
            continue
        return values.get("power_bench_sweep_profile", "core")
    return "core"


def option_value(arguments: list[str], option: str, default: str) -> str:
    """Read an option's value from pass-through arguments without parsing them."""
    for index, argument in enumerate(arguments):
        if argument == option and index + 1 < len(arguments):
            return arguments[index + 1]
        if argument.startswith(f"{option}="):
            return argument.split("=", 1)[1]
    return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="SSH target and host value stored in the database")
    args, suite_args = parser.parse_known_args(argv)

    prohibited = ("--repeats", "--skip-existing", "--no-skip-existing")
    if any(argument == option or argument.startswith(f"{option}=")
           for argument in suite_args for option in prohibited):
        parser.error("this command always uses --repeats 3 --skip-existing")

    inventory = Path(option_value(suite_args, "--inventory", str(DEFAULT_INVENTORY)))
    supplied_sweep = option_value(suite_args, "--sweep", "")
    sweep_args = [] if supplied_sweep else ["--sweep", inventory_sweep(args.host, inventory)]
    command = [
        sys.executable,
        str(ROOT / "run_suite.py"),
        args.host,
        "--repeats", "2",
        "--skip-existing",
        *sweep_args,
        *suite_args,
    ]
    print("+ " + shlex.join(command), flush=True)
    return subprocess.run(command, cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
