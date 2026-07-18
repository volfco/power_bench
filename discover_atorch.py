#!/usr/bin/env python3
"""Discover nearby Atorch BLE power meters and print their MAC addresses."""

import argparse
import asyncio
import sys

from meter_ble import discover_atorch_devices


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="BLE scan duration in seconds (default: 10)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    try:
        devices = asyncio.run(discover_atorch_devices(args.timeout))
    except Exception as exc:
        print(f"Atorch discovery failed: {exc}", file=sys.stderr)
        return 2

    if not devices:
        print("No Atorch devices found.", file=sys.stderr)
        return 1

    print("NAME\tMAC ADDRESS")
    for device in devices:
        print(f"{device.name or '(unknown)'}\t{device.address}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
