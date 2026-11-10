"""Run every on-device diagnostic against every device in the lab, like a nightly fleet sweep."""

from __future__ import annotations

import pytest

from repairlab.device import MODELS, TESTS_BY_COMPONENT

# Spec limits the diagnostics must meet on a healthy device.
SPEC = {
    "battery.health": lambda m: m["max_capacity_pct"] >= 80 and m["cycle_count"] < 1000,
    "battery.charge_rate": lambda m: m["watts"] >= 14.0,
    "display.pixels": lambda m: m["dead_pixels"] == 0,
    "display.touch": lambda m: m["grid_points_ok"] == m["grid_points"],
    "display.true_tone": lambda m: m["delta_e"] < 2.0,
    "camera.focus": lambda m: m["mtf50"] >= 0.35,
    "camera.flash": lambda m: m["lux"] >= 300,
    "biometric.sensor": lambda m: m["match_score"] >= 0.95,
    "speaker.sweep": lambda m: m["thd_pct"] < 1.0,
    "keyboard.keys": lambda m: m["keys_ok"] == m["keys"],
}


def test_diagnostic_passes_within_spec(leased, device_test):
    serial, test_id = device_test
    with leased(serial=serial) as dev:
        result = dev.run_test(test_id)
    assert result["test"] == test_id
    assert result["status"] == "pass", f"{test_id} on {serial} failed: reason={result['reason']}"
    assert SPEC[test_id](result["measurements"]), f"{test_id} out of spec: {result['measurements']}"


def test_device_reports_expected_test_catalog(leased, lab_serial):
    with leased(serial=lab_serial) as dev:
        info = dev.info()
        tests = dev.list_tests()
    expected = [t for c in MODELS[info["model"]]["components"] for t in TESTS_BY_COMPONENT[c]]
    assert tests == expected


def test_all_components_paired_and_calibrated_from_factory(leased, lab_serial):
    with leased(serial=lab_serial) as dev:
        components = dev.info()["components"]
    unfinished = {n: c for n, c in components.items() if not (c["paired"] and c["calibrated"])}
    assert not unfinished, f"factory state wrong: {unfinished}"


@pytest.mark.parametrize("model", list(MODELS))
def test_unknown_test_rejected_with_stable_code(fresh_device, model):
    from repairlab.client import DeviceCommandError

    dev = fresh_device(model)
    with pytest.raises(DeviceCommandError) as exc:
        dev.run_test("flux_capacitor.selftest")
    assert exc.value.code == "UNKNOWN_TEST"


def test_keyboard_test_not_offered_on_phone(fresh_device):
    from repairlab.client import DeviceCommandError

    dev = fresh_device("DV-P1")
    assert "keyboard.keys" not in dev.list_tests()
    with pytest.raises(DeviceCommandError, match="UNKNOWN_TEST"):
        dev.run_test("keyboard.keys")
