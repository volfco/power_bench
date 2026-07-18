#!/usr/bin/env python3
"""Discover nearby Atorch Classic Bluetooth SPP devices."""

import argparse
from dataclasses import dataclass
import math
import re
import subprocess
import sys


SPP_DEVICE_NAME_PREFIXES = ("UD18", "AT24", "J7", "DL24", "S1BWT")
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_SCAN_DEVICE = re.compile(r"\bDevice ([0-9A-Fa-f:]{17})\b")
_CACHED_DEVICE = re.compile(r"^Device ([0-9A-Fa-f:]{17}) (.+)$")


@dataclass(frozen=True)
class ClassicDevice:
    name: str
    address: str


def is_atorch_spp_name(name: str) -> bool:
    """Return whether a Classic Bluetooth name matches an Atorch family."""
    upper_name = name.upper()
    return upper_name.endswith("_SPP") and any(
        upper_name.startswith(prefix) for prefix in SPP_DEVICE_NAME_PREFIXES
    )


def _run_bluetoothctl(arguments: list[str], timeout: float) -> str:
    try:
        result = subprocess.run(
            ["bluetoothctl", *arguments],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("bluetoothctl is required for Classic Bluetooth discovery") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("bluetoothctl did not finish within the expected time") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"bluetoothctl failed: {detail}")
    return _ANSI_ESCAPE.sub("", result.stdout)


def discover_atorch_spp_devices(timeout: float = 10.0) -> list[ClassicDevice]:
    """Run a BR/EDR inquiry and return Atorch SPP devices seen during it."""
    scan_seconds = max(1, math.ceil(timeout))
    scan_output = _run_bluetoothctl(
        ["--timeout", str(scan_seconds), "scan", "bredr"],
        timeout=scan_seconds + 5,
    )
    seen_addresses = {
        match.group(1).upper() for match in _SCAN_DEVICE.finditer(scan_output)
    }

    devices_output = _run_bluetoothctl(["devices"], timeout=5)
    matches = []
    for line in devices_output.splitlines():
        match = _CACHED_DEVICE.match(line.strip())
        if not match:
            continue
        address, name = match.groups()
        address = address.upper()
        if address in seen_addresses and is_atorch_spp_name(name):
            matches.append(ClassicDevice(name=name, address=address))

    return sorted(matches, key=lambda device: (device.name.casefold(), device.address))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Classic Bluetooth scan duration in seconds (default: 10)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    try:
        devices = discover_atorch_spp_devices(args.timeout)
    except Exception as exc:
        print(f"Atorch SPP discovery failed: {exc}", file=sys.stderr)
        return 2

    if not devices:
        print("No Atorch SPP devices found.", file=sys.stderr)
        return 1

    print("NAME\tMAC ADDRESS")
    for device in devices:
        print(f"{device.name}\t{device.address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
