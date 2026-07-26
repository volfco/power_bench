"""Normalize benchmark host hardware into stable report dimensions."""

from __future__ import annotations

import re
from typing import Any, Mapping


LAPTOP_CHASSIS_TYPES = {8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32}
SERVER_CHASSIS_TYPES = {17, 23, 28, 29}
MINI_PC_CHASSIS_TYPES = {3, 4, 6, 7, 13, 15, 16, 24, 35, 36}


def _text(value: Any) -> str:
    return str(value or "").strip()


def classify_host(
    chassis_type: Any = None,
    system_vendor: Any = None,
    system_model: Any = None,
    cpu_model: Any = None,
) -> str:
    """Return a coarse, portable chassis category."""
    try:
        chassis = int(_text(chassis_type).split()[0])
    except (ValueError, IndexError):
        chassis = None
    if chassis in LAPTOP_CHASSIS_TYPES:
        return "Laptop"
    if chassis in SERVER_CHASSIS_TYPES:
        return "Server"

    identity = " ".join(
        (_text(system_vendor), _text(system_model), _text(cpu_model))
    ).lower()
    if re.search(r"\b(server|poweredge|proliant|thinksystem|epyc|xeon)\b", identity):
        return "Server"
    if re.search(
        r"\b(laptop|notebook|ultrabook|thinkpad|latitude|elitebook|"
        r"probook|macbook|vivobook|zenbook)\b",
        identity,
    ):
        return "Laptop"
    if chassis in MINI_PC_CHASSIS_TYPES or re.search(
        r"\b(mini|nuc|optiplex micro|prodesk mini|elitedesk mini|tiny|"
        r"minisforum|beelink|geekom)\b",
        identity,
    ):
        return "Mini PC"
    return "Unclassified"


def hardware_generation(cpu_model: Any) -> str:
    """Extract a deliberately rough, human-readable CPU generation."""
    cpu = _text(cpu_model)
    if not cpu:
        return "Unknown"

    match = re.search(r"\bCore\s+Ultra\s+([3579])\s+(\d{3})", cpu, re.I)
    if match:
        return f"Intel Core Ultra Series {match.group(2)[0]}"

    match = re.search(r"\bCore\(TM\)?\s+i[3579][-\s](\d{4,5})", cpu, re.I)
    if match:
        digits = match.group(1)
        generation = int(digits[:2]) if len(digits) == 5 else int(digits[0])
        suffix = "th" if 10 <= generation % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(generation % 10, "th")
        return f"Intel Core {generation}{suffix} Gen"

    match = re.search(r"\b(?:Processor\s+)?N(95|97|100|200|300|305)\b", cpu, re.I)
    if match and "intel" in cpu.lower():
        return "Intel Alder Lake-N (12th Gen)"

    match = re.search(r"\bXeon\b.*?\b([EGWDL]-?\d{4,5}|\d{4,5})\b", cpu, re.I)
    if match:
        digits = re.sub(r"\D", "", match.group(1))
        return f"Intel Xeon {digits[:2]}00 series" if len(digits) >= 4 else "Intel Xeon"

    match = re.search(r"\bRyzen\s+(?:[3579]\s+)?(?:PRO\s+)?(\d{4})", cpu, re.I)
    if match:
        return f"AMD Ryzen {match.group(1)[0]}000 series"

    match = re.search(r"\bEPYC\s+(\d{4})", cpu, re.I)
    if match:
        generation = {"1": "1st", "2": "2nd", "3": "3rd", "4": "4th", "5": "5th"}.get(
            match.group(1)[-1]
        )
        return f"AMD EPYC {generation} Gen" if generation else "AMD EPYC"

    match = re.search(r"\bApple\s+(M\d(?:\s+(?:Pro|Max|Ultra))?)\b", cpu, re.I)
    if match:
        return f"Apple {match.group(1)}"

    return cpu


def enrich_host_metadata(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return extracted host dimensions, preserving explicitly recorded values."""
    group = _text(values.get("host_group")) or classify_host(
        values.get("chassis_type"),
        values.get("system_vendor"),
        values.get("system_model"),
        values.get("cpu_model"),
    )
    generation = _text(values.get("hardware_generation")) or hardware_generation(
        values.get("cpu_model")
    )
    return {"host_group": group, "hardware_generation": generation}
