"""Parse Phoronix Test Suite composite.xml result files.

PTS writes each saved result set to
``~/.phoronix-test-suite/test-results/<name>/composite.xml``. We pull that file
back over SSH and extract the score, its unit (``Scale``), and its direction
(``Proportion``: HIB = higher-is-better, LIB = lower-is-better) so performance can be
joined with the power data.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass


@dataclass
class PtsResult:
    title: str
    scale: str         # unit, e.g. "Seconds", "Tokens Per Second"
    proportion: str    # "HIB" or "LIB"
    value: float       # averaged result value

    @property
    def higher_is_better(self) -> bool:
        # PTS uses "LIB" for lower-is-better; everything else (incl. "HIB") is HIB.
        return self.proportion.strip().upper() != "LIB"


def _to_float(text):
    """Parse a result value. Handles a plain number or ``:``/``,``-separated samples."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        nums = []
        for part in re.split(r"[:,]", text):
            part = part.strip()
            try:
                nums.append(float(part))
            except ValueError:
                continue
        return sum(nums) / len(nums) if nums else None


def parse_composite_xml(xml_text: str):
    """Return a list of :class:`PtsResult` parsed from a composite.xml string.

    A single test profile yields one Result; a suite yields several.
    """
    root = ET.fromstring(xml_text)
    results = []
    for res in root.findall(".//Result"):
        values = []
        for entry in res.findall("./Data/Entry"):
            v = _to_float(entry.findtext("Value"))
            if v is None:
                v = _to_float(entry.findtext("RawString"))
            if v is not None:
                values.append(v)
        if not values:
            continue
        results.append(
            PtsResult(
                title=(res.findtext("Title") or "").strip(),
                scale=(res.findtext("Scale") or "").strip(),
                proportion=(res.findtext("Proportion") or "").strip(),
                value=sum(values) / len(values),
            )
        )
    return results
