"""Chaos scenario: make the lab misbehave the way a real device lab does.

Enabled with REPAIRLAB_CHAOS=1. REPAIRLAB_ATTEMPT tells the scenario which CI attempt
this is, so transient faults appear only on the first attempt (like a sensor that
glitches once) while hardware and infrastructure faults persist across reruns.

The scenario gives the triage engine one of each failure class to sort out:
    product  – a device with a genuinely degraded battery
    infra    – a device whose link drops on every connection
    flaky    – a touch sensor that fails on attempt 1 and passes on the rerun
"""

from __future__ import annotations

import os

from .agent import Lab
from .client import DiagClient


def enabled() -> bool:
    return os.environ.get("REPAIRLAB_CHAOS", "") not in ("", "0")


def attempt() -> int:
    return int(os.environ.get("REPAIRLAB_ATTEMPT", "1"))


def apply(lab: Lab) -> dict[str, str]:
    """Inject the scenario's faults. Returns {serial: fault description}."""
    by_model: dict[str, list] = {}
    for rec in lab.inventory():
        by_model.setdefault(rec.model, []).append(rec)

    plan: list[tuple[object, dict, str]] = [
        (by_model["DV-L1"][-1], {"kind": "degraded", "target": "battery", "value": 55.0}, "degraded battery (product)"),
        (by_model["DV-P1"][-1], {"kind": "drop_connection"}, "link drops (infra)"),
    ]
    if attempt() == 1:
        plan.append((by_model["DV-T1"][0], {"kind": "intermittent", "target": "display.touch", "probability": 1.0},
                     "touch sensor glitch (transient)"))

    applied = {}
    for rec, fault, desc in plan:
        with DiagClient(rec.host, rec.port) as c:  # type: ignore[attr-defined]
            c.call("inject_fault", **fault)
        applied[rec.serial] = desc  # type: ignore[attr-defined]
    return applied
