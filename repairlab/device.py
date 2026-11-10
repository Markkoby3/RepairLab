"""Simulated device under test.

Each SimDevice models a consumer device with replaceable components, an on-device
diagnostic suite, and a repair mode in which parts can be replaced, paired to the
logic board, and calibrated. Faults can be injected to reproduce the failure modes a
real automation lab sees: real hardware defects, intermittent sensors, slow links,
and dropped connections.
"""

from __future__ import annotations

import random
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable

# Fictional device families. Each lists the components it ships with.
MODELS: dict[str, dict[str, Any]] = {
    "DV-P1": {"family": "phone", "components": ["battery", "display", "camera", "biometric", "speaker"]},
    "DV-T1": {"family": "tablet", "components": ["battery", "display", "camera", "speaker"]},
    "DV-L1": {"family": "laptop", "components": ["battery", "display", "keyboard", "speaker"]},
}

# Which components must be paired to the logic board / calibrated after replacement.
NEEDS_PAIRING = {"battery", "display", "camera", "biometric"}
NEEDS_CALIBRATION = {"display", "camera", "biometric"}

# Diagnostic tests available per component.
TESTS_BY_COMPONENT: dict[str, list[str]] = {
    "battery": ["battery.health", "battery.charge_rate"],
    "display": ["display.pixels", "display.touch", "display.true_tone"],
    "camera": ["camera.focus", "camera.flash"],
    "biometric": ["biometric.sensor"],
    "speaker": ["speaker.sweep"],
    "keyboard": ["keyboard.keys"],
}


class DeviceError(Exception):
    """A diagnostic protocol error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def genuine_part_serial(component: str, n: int) -> str:
    """Build a part serial that passes the genuine-part checksum."""
    body = f"{component[:3].upper()}{n:06d}"
    return f"GP-{body}-{_checksum(body)}"


def _checksum(body: str) -> str:
    return str(sum(ord(c) * (i + 1) for i, c in enumerate(body)) % 10)


def is_genuine(part_serial: str, component: str) -> bool:
    parts = part_serial.split("-")
    if len(parts) != 3 or parts[0] != "GP":
        return False
    body, check = parts[1], parts[2]
    return body.startswith(component[:3].upper()) and _checksum(body) == check


@dataclass
class Component:
    name: str
    serial: str
    health: float = 100.0  # percent
    cycle_count: int = 0
    paired: bool = True
    calibrated: bool = True


@dataclass
class Fault:
    kind: str  # "intermittent", "latency", "drop_connection", "hung", "degraded"
    target: str | None = None  # test id or component name
    probability: float = 1.0
    latency_ms: int = 0
    value: float | None = None


@dataclass
class SimDevice:
    serial: str
    model: str
    firmware: str = "24.1.0"
    seed: int = 0
    components: dict[str, Component] = field(default_factory=dict)
    faults: list[Fault] = field(default_factory=list)
    repair_mode: bool = False
    boot_count: int = 1
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.model not in MODELS:
            raise ValueError(f"unknown model {self.model}")
        self._rng = random.Random(self.seed)
        if not self.components:
            for i, name in enumerate(MODELS[self.model]["components"]):
                self.components[name] = Component(
                    name=name,
                    serial=genuine_part_serial(name, zlib.crc32(f"{self.serial}:{i}".encode()) % 1_000_000),
                    cycle_count=self._rng.randint(20, 400) if name == "battery" else 0,
                    health=self._rng.uniform(88, 99) if name == "battery" else 100.0,
                )

    # ------------------------------------------------------------------ info
    def info(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "model": self.model,
            "family": MODELS[self.model]["family"],
            "firmware": self.firmware,
            "repair_mode": self.repair_mode,
            "boot_count": self.boot_count,
            "components": {
                c.name: {
                    "serial": c.serial,
                    "paired": c.paired,
                    "calibrated": c.calibrated,
                    "health": round(c.health, 1),
                }
                for c in self.components.values()
            },
        }

    def list_tests(self) -> list[str]:
        return [t for c in self.components for t in TESTS_BY_COMPONENT[c]]

    # ------------------------------------------------------------ diagnostics
    def run_test(self, test_id: str) -> dict[str, Any]:
        if test_id not in self.list_tests():
            raise DeviceError("UNKNOWN_TEST", f"{test_id} not supported on {self.model}")
        component = self.components[test_id.split(".")[0]]
        start = time.perf_counter()

        status, measurements, reason = self._evaluate(test_id, component)

        for fault in self._faults_for(test_id, component.name):
            if fault.kind == "intermittent" and self._rng.random() < fault.probability:
                status, reason = "fail", "SENSOR_NO_RESPONSE"

        return {
            "test": test_id,
            "status": status,
            "reason": reason,
            "measurements": measurements,
            "duration_ms": round((time.perf_counter() - start) * 1000, 3),
        }

    def _evaluate(self, test_id: str, c: Component) -> tuple[str, dict[str, Any], str | None]:
        if c.name in NEEDS_PAIRING and not c.paired:
            return "fail", {}, "UNPAIRED_PART"
        if c.name in NEEDS_CALIBRATION and not c.calibrated:
            return "fail", {}, "NOT_CALIBRATED"

        checks: dict[str, Callable[[], tuple[bool, dict[str, Any]]]] = {
            "battery.health": lambda: (
                c.health >= 80 and c.cycle_count < 1000,
                {"max_capacity_pct": round(c.health, 1), "cycle_count": c.cycle_count},
            ),
            "battery.charge_rate": lambda: (c.health >= 70, {"watts": round(20 * c.health / 100, 2)}),
            "display.pixels": lambda: (True, {"dead_pixels": 0}),
            "display.touch": lambda: (True, {"grid_points_ok": 120, "grid_points": 120}),
            "display.true_tone": lambda: (True, {"delta_e": 1.2}),
            "camera.focus": lambda: (True, {"mtf50": 0.41}),
            "camera.flash": lambda: (True, {"lux": 410}),
            "biometric.sensor": lambda: (True, {"match_score": 0.98}),
            "speaker.sweep": lambda: (True, {"thd_pct": 0.8}),
            "keyboard.keys": lambda: (True, {"keys_ok": 78, "keys": 78}),
        }
        ok, measurements = checks[test_id]()
        return ("pass" if ok else "fail"), measurements, (None if ok else "OUT_OF_SPEC")

    def _faults_for(self, test_id: str, component: str) -> list[Fault]:
        return [f for f in self.faults if f.target in (None, test_id, component)]

    # ------------------------------------------------------------------ repair
    def enter_repair_mode(self) -> None:
        self.repair_mode = True

    def exit_repair_mode(self) -> None:
        unfinished = [
            c.name for c in self.components.values()
            if (c.name in NEEDS_PAIRING and not c.paired)
            or (c.name in NEEDS_CALIBRATION and not c.calibrated)
        ]
        if unfinished:
            raise DeviceError("REPAIR_INCOMPLETE", f"components not finalized: {', '.join(unfinished)}")
        self.repair_mode = False

    def replace_part(self, component: str, part_serial: str) -> dict[str, Any]:
        self._require_repair_mode()
        c = self._component(component)
        if not is_genuine(part_serial, component):
            raise DeviceError("NON_GENUINE_PART", f"{part_serial} failed authentication for {component}")
        c.serial = part_serial
        c.health = 100.0
        c.cycle_count = 0
        c.paired = component not in NEEDS_PAIRING
        c.calibrated = component not in NEEDS_CALIBRATION
        # A replaced part clears any degradation fault on that component.
        self.faults = [f for f in self.faults if not (f.kind == "degraded" and f.target == component)]
        return {"component": component, "serial": part_serial, "paired": c.paired, "calibrated": c.calibrated}

    def pair(self, component: str) -> dict[str, Any]:
        self._require_repair_mode()
        c = self._component(component)
        c.paired = True
        return {"component": component, "paired": True}

    def calibrate(self, component: str) -> dict[str, Any]:
        self._require_repair_mode()
        c = self._component(component)
        if component in NEEDS_PAIRING and not c.paired:
            raise DeviceError("PAIRING_REQUIRED", f"pair {component} before calibration")
        c.calibrated = True
        return {"component": component, "calibrated": True}

    def reboot(self) -> dict[str, Any]:
        self.boot_count += 1
        self.repair_mode = False
        # Transient faults do not survive a reboot; hardware degradation does.
        self.faults = [f for f in self.faults if f.kind == "degraded"]
        return {"boot_count": self.boot_count}

    # ------------------------------------------------------------------ faults
    def inject_fault(self, fault: Fault) -> None:
        if fault.kind == "degraded":
            c = self._component(fault.target or "battery")
            c.health = fault.value if fault.value is not None else 62.0
        self.faults.append(fault)

    def clear_faults(self) -> None:
        self.faults.clear()

    def latency_ms(self) -> int:
        return max((f.latency_ms for f in self.faults if f.kind == "latency"), default=0)

    def is_hung(self) -> bool:
        return any(f.kind == "hung" for f in self.faults)

    def should_drop(self) -> bool:
        return any(f.kind == "drop_connection" for f in self.faults)

    # ----------------------------------------------------------------- helpers
    def _require_repair_mode(self) -> None:
        if not self.repair_mode:
            raise DeviceError("NOT_IN_REPAIR_MODE", "enter repair mode first")

    def _component(self, name: str) -> Component:
        if name not in self.components:
            raise DeviceError("UNKNOWN_COMPONENT", f"{self.model} has no {name}")
        return self.components[name]
