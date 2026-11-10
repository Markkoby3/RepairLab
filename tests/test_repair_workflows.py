"""End-to-end repair workflows: diagnose -> replace -> pair -> calibrate -> verify -> release."""

from __future__ import annotations

import pytest

from repairlab.client import DeviceCommandError
from repairlab.device import MODELS, NEEDS_CALIBRATION, NEEDS_PAIRING, genuine_part_serial
from repairlab.workflows import IllegalTransition, RepairWorkflow, State, required_steps

ALL_REPAIRS = [(m, c) for m, spec in MODELS.items() for c in spec["components"]]


@pytest.mark.parametrize("model,component", ALL_REPAIRS, ids=[f"{m}-{c}" for m, c in ALL_REPAIRS])
def test_forced_part_replacement_completes_on_every_component(fresh_device, model, component):
    dev = fresh_device(model)
    result = RepairWorkflow(dev, component).run(genuine_part_serial(component, 1), force_replace=True)

    assert result.succeeded, result.log
    visited = [entry["state"] for entry in result.log]
    for step in required_steps(component):
        assert step.value in visited, f"{component} skipped {step.value}"
    after = dev.info()["components"][component]
    assert after["serial"] == genuine_part_serial(component, 1)
    assert after["paired"] and after["calibrated"]


def test_degraded_battery_is_detected_then_fixed_by_replacement(fresh_device):
    dev = fresh_device("DV-P1")
    dev.call("inject_fault", kind="degraded", target="battery", value=61.0)

    result = RepairWorkflow(dev, "battery").run(genuine_part_serial("battery", 7))

    assert result.succeeded
    assert result.log[0] == {"state": "diagnosed", "failing": ["battery.health", "battery.charge_rate"]}
    assert dev.run_test("battery.health")["measurements"]["max_capacity_pct"] == 100.0


def test_healthy_device_released_as_no_fault_found(fresh_device):
    dev = fresh_device("DV-T1")
    result = RepairWorkflow(dev, "display").run(genuine_part_serial("display", 3))
    assert result.succeeded
    assert [e["state"] for e in result.log] == ["diagnosed", "released"]
    assert dev.info()["repair_mode"] is False


def test_non_genuine_part_is_rejected_and_workflow_fails(fresh_device):
    dev = fresh_device("DV-P1")
    good = genuine_part_serial("display", 1)
    counterfeit = good[:-1] + str((int(good[-1]) + 1) % 10)  # same part, bad checksum
    result = RepairWorkflow(dev, "display").run(counterfeit, force_replace=True)
    assert result.state == State.FAILED
    assert result.error_code == "NON_GENUINE_PART"


def test_part_for_wrong_component_is_rejected(fresh_device):
    dev = fresh_device("DV-P1")
    dev.call("enter_repair_mode")
    with pytest.raises(DeviceCommandError) as exc:
        dev.call("replace_part", component="battery", part_serial=genuine_part_serial("display", 5))
    assert exc.value.code == "NON_GENUINE_PART"


@pytest.mark.parametrize("component", sorted(NEEDS_PAIRING))
def test_unpaired_part_fails_diagnostics_until_paired(fresh_device, component):
    model = next(m for m, s in MODELS.items() if component in s["components"])
    dev = fresh_device(model)
    dev.call("enter_repair_mode")
    dev.call("replace_part", component=component, part_serial=genuine_part_serial(component, 9))

    test_id = next(t for t in dev.list_tests() if t.startswith(component + "."))
    assert dev.run_test(test_id)["reason"] == "UNPAIRED_PART"
    dev.call("pair", component=component)
    if component in NEEDS_CALIBRATION:
        assert dev.run_test(test_id)["reason"] == "NOT_CALIBRATED"
        dev.call("calibrate", component=component)
    assert dev.run_test(test_id)["status"] == "pass"


def test_calibration_before_pairing_is_refused(fresh_device):
    dev = fresh_device("DV-P1")
    dev.call("enter_repair_mode")
    dev.call("replace_part", component="camera", part_serial=genuine_part_serial("camera", 2))
    with pytest.raises(DeviceCommandError) as exc:
        dev.call("calibrate", component="camera")
    assert exc.value.code == "PAIRING_REQUIRED"


def test_cannot_leave_repair_mode_with_unfinished_repair(fresh_device):
    dev = fresh_device("DV-T1")
    dev.call("enter_repair_mode")
    dev.call("replace_part", component="display", part_serial=genuine_part_serial("display", 4))
    with pytest.raises(DeviceCommandError) as exc:
        dev.call("exit_repair_mode")
    assert exc.value.code == "REPAIR_INCOMPLETE"
    assert "display" in exc.value.message


def test_repair_commands_require_repair_mode(fresh_device):
    dev = fresh_device("DV-L1")
    with pytest.raises(DeviceCommandError) as exc:
        dev.call("replace_part", component="keyboard", part_serial=genuine_part_serial("keyboard", 1))
    assert exc.value.code == "NOT_IN_REPAIR_MODE"


def test_reboot_exits_repair_mode_and_preserves_hardware_degradation(fresh_device):
    dev = fresh_device("DV-L1")
    dev.call("inject_fault", kind="degraded", target="battery", value=50.0)
    dev.call("inject_fault", kind="intermittent", target="speaker.sweep", probability=1.0)
    dev.call("enter_repair_mode")
    dev.reboot()
    info = dev.info()
    assert info["repair_mode"] is False and info["boot_count"] == 2
    assert dev.run_test("speaker.sweep")["status"] == "pass"  # transient fault cleared
    assert dev.run_test("battery.health")["status"] == "fail"  # hardware still bad


def test_workflow_rejects_illegal_transition(fresh_device):
    wf = RepairWorkflow(fresh_device("DV-P1"), "battery")
    with pytest.raises(IllegalTransition):
        wf._to(State.VERIFIED)


def test_required_steps_table():
    assert required_steps("battery") == [State.PAIRED]
    assert required_steps("display") == [State.PAIRED, State.CALIBRATED]
    assert required_steps("speaker") == []
