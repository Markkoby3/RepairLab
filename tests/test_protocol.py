"""Wire protocol robustness and contract conformance.

contracts/ holds golden request/response pairs. This suite proves the live device agent
still produces them; the Swift DiagKit XCTest suite decodes the same files, so a protocol
change that would break the Swift client fails here first.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from repairlab.agent import Lab
from repairlab.client import DeviceCommandError, DiagClient
from repairlab.workflows import TRANSITIONS, State, required_steps

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"
PROTOCOL_FIXTURES = sorted((CONTRACTS / "protocol").glob("*.json"))


def _shape(v):
    if isinstance(v, dict):
        return {k: _shape(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_shape(v[0])] if v else []
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    return type(v).__name__


def _get(obj, dotted):
    for part in dotted.split("."):
        obj = obj[part]
    return obj


@pytest.fixture
def raw_device():
    with Lab(models=["DV-P1"], per_model=1, seed=99) as lab:
        rec = lab.inventory()[0]
        sock = socket.create_connection((rec.host, rec.port), timeout=1.0)
        f = sock.makefile("rwb")

        def send(obj_or_bytes):
            data = obj_or_bytes if isinstance(obj_or_bytes, bytes) else json.dumps(obj_or_bytes).encode()
            f.write(data + b"\n")
            f.flush()
            return json.loads(f.readline())

        yield send
        f.close()
        sock.close()


@pytest.mark.parametrize("fixture", PROTOCOL_FIXTURES, ids=[p.stem for p in PROTOCOL_FIXTURES])
def test_live_agent_matches_contract(raw_device, fixture):
    contract = json.loads(fixture.read_text())
    for i, step in enumerate(contract["setup"]):
        assert "error" not in raw_device({"id": 100 + i, **step})
    live = raw_device(contract["request"])

    assert _shape(live) == _shape(contract["response"]), "response shape drifted from contract"
    for path in contract["exact"]:
        assert _get(live, path) == _get(contract["response"], path), path


def test_workflow_contract_matches_python_state_machine():
    contract = json.loads((CONTRACTS / "workflow.json").read_text())
    assert contract["states"] == [s.value for s in State]
    assert contract["transitions"] == {s.value: sorted(t.value for t in v) for s, v in TRANSITIONS.items()}
    for component, steps in contract["required_steps"].items():
        assert [s.value for s in required_steps(component)] == steps


def test_malformed_json_gets_bad_request_and_session_survives(raw_device):
    assert raw_device(b"{not json")["error"]["code"] == "BAD_REQUEST"
    assert raw_device({"id": 2, "method": "hello"})["result"]["protocol"] == "1.2"


def test_request_without_method_is_rejected(raw_device):
    assert raw_device({"id": 1})["error"]["code"] == "BAD_REQUEST"


def test_missing_param_is_reported(raw_device):
    resp = raw_device({"id": 9, "method": "run_test", "params": {}})
    assert resp == {"id": 9, "error": {"code": "MISSING_PARAM", "message": "missing required param 'test_id'"}}


def test_bad_fault_params_do_not_crash_agent(raw_device):
    assert raw_device({"id": 1, "method": "inject_fault", "params": {"bogus": 1}})["error"]["code"] == "BAD_PARAMS"
    assert raw_device({"id": 2, "method": "hello"})["id"] == 2


def test_response_ids_echo_request_ids(raw_device):
    for req_id in (1, 42, 7_000_000):
        assert raw_device({"id": req_id, "method": "hello"})["id"] == req_id


def test_client_raises_command_error_with_code(fresh_device):
    dev = fresh_device("DV-P1")
    with pytest.raises(DeviceCommandError) as exc:
        dev.call("pair", component="display")
    assert exc.value.code == "NOT_IN_REPAIR_MODE"


def test_slow_link_raises_device_timeout(fresh_device):
    from repairlab.client import DeviceTimeout

    dev = fresh_device("DV-T1")
    dev.call("inject_fault", kind="latency", latency_ms=1500)
    with pytest.raises(DeviceTimeout):
        dev.hello()


def test_connect_to_dead_port_raises_disconnected():
    from repairlab.client import DeviceDisconnected

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(DeviceDisconnected):
        DiagClient("127.0.0.1", port, timeout=0.5).connect()
