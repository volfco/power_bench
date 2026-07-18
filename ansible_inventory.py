"""Resolve benchmark targets from Ansible inventory aliases."""

from __future__ import annotations

import ipaddress
import json
import subprocess
from dataclasses import dataclass


class InventoryHostError(ValueError):
    """Raised when a requested benchmark host is not a valid inventory alias."""


@dataclass(frozen=True)
class InventoryHost:
    name: str
    address: str
    user: str | None = None
    private_key: str | None = None
    sweep: str = "core"


def resolve_inventory_host(name: str, inventory: str) -> InventoryHost:
    """Return connection data for one exact, non-IP inventory host name."""
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise InventoryHostError(
            f"host must be an Ansible inventory name, not an IP address: {name}"
        )

    try:
        result = subprocess.run(
            ["ansible-inventory", "-i", inventory, "--list"],
            check=True,
            capture_output=True,
            text=True,
        )
        inventory_data = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise InventoryHostError(f"could not read Ansible inventory {inventory}: {exc}") from exc

    hostvars = inventory_data.get("_meta", {}).get("hostvars", {})
    if name not in hostvars:
        available = ", ".join(sorted(hostvars)) or "(none)"
        raise InventoryHostError(
            f"host {name!r} is not in Ansible inventory {inventory}; available hosts: {available}"
        )

    variables = hostvars[name]
    return InventoryHost(
        name=name,
        address=str(variables.get("ansible_host", name)),
        user=variables.get("ansible_user") or variables.get("ansible_ssh_user"),
        private_key=(
            variables.get("ansible_private_key_file")
            or variables.get("ansible_ssh_private_key_file")
        ),
        sweep=variables.get("power_bench_sweep_profile", "core"),
    )
