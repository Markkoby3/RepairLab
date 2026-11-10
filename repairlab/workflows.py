"""Repair workflow: the end-to-end path a technician (or a robot) follows to fix a device.

    INTAKE -> DIAGNOSED -> IN_REPAIR -> PART_REPLACED -> PAIRED -> CALIBRATED -> VERIFIED -> RELEASED

Pairing and calibration are skipped for components that do not need them. Any step
that fails moves the workflow to FAILED with the device error code, which is what the
test suite asserts on. The same state machine is mirrored in Swift (DiagKit) and both
are held to the shared fixtures in contracts/.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .client import DeviceCommandError, DiagClient
from .device import NEEDS_CALIBRATION, NEEDS_PAIRING, TESTS_BY_COMPONENT


class State(str, Enum):
    INTAKE = "intake"
    DIAGNOSED = "diagnosed"
    IN_REPAIR = "in_repair"
    PART_REPLACED = "part_replaced"
    PAIRED = "paired"
    CALIBRATED = "calibrated"
    VERIFIED = "verified"
    RELEASED = "released"
    FAILED = "failed"


TRANSITIONS: dict[State, set[State]] = {
    State.INTAKE: {State.DIAGNOSED},
    State.DIAGNOSED: {State.IN_REPAIR, State.RELEASED},  # no fault found -> release
    State.IN_REPAIR: {State.PART_REPLACED},
    State.PART_REPLACED: {State.PAIRED, State.CALIBRATED, State.VERIFIED},
    State.PAIRED: {State.CALIBRATED, State.VERIFIED},
    State.CALIBRATED: {State.VERIFIED},
    State.VERIFIED: {State.RELEASED},
    State.RELEASED: set(),
    State.FAILED: set(),
}


class IllegalTransition(Exception):
    pass


def required_steps(component: str) -> list[State]:
    """The post-replacement steps a component needs before verification."""
    steps = []
    if component in NEEDS_PAIRING:
        steps.append(State.PAIRED)
    if component in NEEDS_CALIBRATION:
        steps.append(State.CALIBRATED)
    return steps


@dataclass
class WorkflowResult:
    component: str
    state: State = State.INTAKE
    error_code: str | None = None
    failing_tests: list[str] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.state == State.RELEASED and self.error_code is None


class RepairWorkflow:
    def __init__(self, client: DiagClient, component: str):
        self.client = client
        self.component = component
        self.result = WorkflowResult(component=component)

    def _to(self, new: State, **detail: Any) -> None:
        cur = self.result.state
        if new not in TRANSITIONS[cur]:
            raise IllegalTransition(f"{cur.value} -> {new.value}")
        self.result.state = new
        self.result.log.append({"state": new.value, **detail})

    def _fail(self, code: str, **detail: Any) -> WorkflowResult:
        self.result.state = State.FAILED
        self.result.error_code = code
        self.result.log.append({"state": State.FAILED.value, "code": code, **detail})
        return self.result

    def diagnose(self) -> list[str]:
        failing = [
            t for t in TESTS_BY_COMPONENT[self.component]
            if self.client.run_test(t)["status"] != "pass"
        ]
        self.result.failing_tests = failing
        self._to(State.DIAGNOSED, failing=failing)
        return failing

    def run(self, part_serial: str, force_replace: bool = False) -> WorkflowResult:
        """Diagnose, and if needed replace/pair/calibrate/verify. Never raises for device errors."""
        try:
            failing = self.diagnose()
            if not failing and not force_replace:
                self._to(State.RELEASED, note="no fault found")
                return self.result

            self.client.call("enter_repair_mode")
            self._to(State.IN_REPAIR)
            self.client.call("replace_part", component=self.component, part_serial=part_serial)
            self._to(State.PART_REPLACED, part_serial=part_serial)
            for step in required_steps(self.component):
                self.client.call("pair" if step == State.PAIRED else "calibrate", component=self.component)
                self._to(step)
            self.client.call("exit_repair_mode")

            still_failing = [
                t for t in TESTS_BY_COMPONENT[self.component]
                if self.client.run_test(t)["status"] != "pass"
            ]
            if still_failing:
                self.result.failing_tests = still_failing
                return self._fail("VERIFICATION_FAILED", failing=still_failing)
            self._to(State.VERIFIED)
            self._to(State.RELEASED)
            return self.result
        except DeviceCommandError as e:
            return self._fail(e.code, message=e.message)
