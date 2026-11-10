"""Triage engine: classification rules, clustering, flake detection, LLM guardrails, and an
end-to-end run against a deliberately misbehaving lab."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from repairlab import llm
from repairlab.triage import Category, Outcome, classify, parse_junit, signature, triage

ROOT = Path(__file__).resolve().parent.parent


def junit(tmp_path: Path, name: str, cases: list[tuple[str, str, str]]) -> Path:
    """cases: (name, status, message) with status in passed/failure/error/skipped."""
    body = []
    for case, status, msg in cases:
        inner = "" if status == "passed" else f'<{status} message="{msg}">{msg}\ntraceback…</{status}>'
        body.append(f'<testcase classname="tests.test_x" name="{case}" time="0.01">{inner}</testcase>')
    path = tmp_path / name
    path.write_text(f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{"".join(body)}</testsuite></testsuites>')
    return path


@pytest.mark.parametrize("message,status,expected", [
    ("repairlab.client.DeviceTimeout: hello timed out after 1.0s", "failed", Category.INFRA),
    ("repairlab.client.DeviceDisconnected: device closed the connection", "failed", Category.INFRA),
    ("repairlab.fleet.LeaseTimeout: No healthy device in fleet", "failed", Category.INFRA),
    ("AttributeError: 'DiagClient' object has no attribute 'run_tset'", "failed", Category.TEST),
    ("fixture 'lab_robot' not found", "error", Category.TEST),
    ("AssertionError: battery.health on DVL107001 failed", "failed", Category.PRODUCT),
    ("repairlab.client.DeviceCommandError: REPAIR_INCOMPLETE: display", "failed", Category.PRODUCT),
    ("AssertionError: assert 1 == 2", "error", Category.TEST),  # assertion inside fixture setup
    ("Segmentation fault in helper", "failed", Category.UNKNOWN),
])
def test_classification_rules(message, status, expected):
    assert classify(Outcome("t::x", status, message))[0] == expected


def test_signature_normalizes_devices_tests_and_numbers():
    a = Outcome("t", "failed", "AssertionError: battery.health on DVL107001 failed after 3.2s (0x1F)")
    b = Outcome("t", "failed", "AssertionError: display.touch on DVP107000 failed after 15s (0xAA)")
    assert signature(a) == signature(b) == "AssertionError: <DIAG> on <DEVICE> failed after <N>s (<HEX>)"


def test_parse_junit_maps_classname_to_nodeid(tmp_path):
    p = junit(tmp_path, "a.xml", [("test_ok", "passed", ""), ("test_bad[DVP107000]", "failure", "AssertionError: x"),
                                  ("test_skip", "skipped", "nope")])
    out = {o.nodeid: o for o in parse_junit(p)}
    assert out["tests/test_x.py::test_ok"].status == "passed"
    assert out["tests/test_x.py::test_bad[DVP107000]"].status == "failed"
    assert out["tests/test_x.py::test_bad[DVP107000]"].devices == {"DVP107000"}
    assert out["tests/test_x.py::test_skip"].status == "skipped"


def test_fail_then_pass_on_rerun_is_flaky_not_product(tmp_path):
    a1 = junit(tmp_path, "1.xml", [("test_t[DVT107000-display.touch]", "failure", "AssertionError: sensor"),
                                   ("test_ok", "passed", "")])
    a2 = junit(tmp_path, "2.xml", [("test_t[DVT107000-display.touch]", "passed", "")])
    report = triage([parse_junit(a1), parse_junit(a2)])
    assert [c.category for c in report.clusters] == [Category.FLAKY]
    assert report.verdict == "green-with-flakes" and report.exit_code == 0
    assert report.passed == report.total == 2


def test_failure_on_both_attempts_stays_product(tmp_path):
    case = [("test_t[DVL107001-battery.health]", "failure", "AssertionError: battery.health on DVL107001")]
    report = triage([parse_junit(junit(tmp_path, "1.xml", case)), parse_junit(junit(tmp_path, "2.xml", case))])
    assert report.clusters[0].category == Category.PRODUCT
    assert report.verdict == "red" and report.exit_code == 1


def test_infra_only_run_exits_2(tmp_path):
    p = junit(tmp_path, "1.xml", [("test_a[DVP107001-x]", "failure", "LeaseTimeout: No healthy device"),
                                  ("test_b[DVP107001-y]", "failure", "LeaseTimeout: No healthy device")])
    report = triage([parse_junit(p)])
    assert len(report.clusters) == 1  # infra clusters across test functions
    assert report.verdict == "infra" and report.exit_code == 2


def test_blast_radius_distinguishes_hardware_from_regression(tmp_path):
    one_device = [(f"test_d[DVL107001-t{i}]", "failure", "AssertionError: OUT_OF_SPEC") for i in range(3)]
    many_devices = [(f"test_e[DVP10700{i}-x]", "failure", "AssertionError: crc mismatch") for i in range(3)]
    report = triage([parse_junit(junit(tmp_path, "1.xml", one_device + many_devices))])
    radius = {c.function.split("::")[-1]: c.blast_radius for c in report.clusters}
    assert radius == {"test_d": "single-device", "test_e": "multi-device"}
    hints = {c.blast_radius: c.next_step for c in report.clusters}
    assert "hardware" in hints["single-device"] and "regression" in hints["multi-device"]


def test_markdown_report_orders_product_first(tmp_path):
    p = junit(tmp_path, "1.xml", [("test_a[DVP107001-x]", "failure", "LeaseTimeout: No healthy device"),
                                  ("test_b[DVL107001-y]", "failure", "AssertionError: bad")])
    md = triage([parse_junit(p)]).to_markdown()
    assert md.index("[product]") < md.index("[infra]")
    assert md.startswith("## 🔴 Test triage: red")


# ---------------------------------------------------------------- LLM guardrails
def _report(tmp_path):
    return triage([parse_junit(junit(tmp_path, "1.xml", [("test_b[DVL107001-y]", "failure", "AssertionError: bad")]))])


def test_ai_notes_disabled_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.ai_notes(_report(tmp_path)) is None


def test_ai_notes_drop_hallucinated_clusters_and_keep_categories(tmp_path):
    report = _report(tmp_path)
    sent = {}

    def fake_transport(payload):
        sent.update(payload)
        reply = {"summary": "One unit has a weak battery.",
                 "clusters": [{"id": "C1", "root_cause": "cell wear", "next_step": "swap battery"},
                              {"id": "C99", "root_cause": "invented", "next_step": "ignore"}]}
        return {"content": [{"type": "text", "text": "```json\n" + json.dumps(reply) + "\n```"}]}

    notes = llm.ai_notes(report, transport=fake_transport)
    assert "C1" in notes and "C99" not in notes and "cell wear" in notes
    assert report.clusters[0].category == Category.PRODUCT
    prompt = json.loads(sent["messages"][0]["content"])
    assert prompt["clusters"][0]["id"] == "C1" and len(prompt["clusters"][0]["sample"]) <= 600


def test_ai_notes_survive_garbage_response(tmp_path):
    assert llm.ai_notes(_report(tmp_path), transport=lambda p: {"content": [{"type": "text", "text": "sorry!"}]}) is None


# ------------------------------------------------------------------- end to end
@pytest.mark.skipif(os.environ.get("REPAIRLAB_CHAOS") not in (None, "", "0"), reason="already inside a chaos run")
def test_demo_sorts_chaos_faults_into_the_right_buckets(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "repairlab", "demo", "--out", str(tmp_path)],
                          cwd=ROOT, capture_output=True, text=True, timeout=180,
                          env={k: v for k, v in os.environ.items() if not k.startswith("REPAIRLAB_")})
    assert proc.returncode == 0, proc.stderr
    report = json.loads((tmp_path / "triage.json").read_text())
    by_cat = {}
    for c in report["clusters"]:
        by_cat.setdefault(c["category"], set()).update(c["devices"])
    assert by_cat == {"product": {"DVL107001"}, "infra": {"DVP107001"}, "flaky": {"DVT107000"}}
    assert report["verdict"] == "red"
