from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Callable, Iterator

import pytest

from repairlab import chaos
from repairlab.agent import Lab
from repairlab.client import DiagClient
from repairlab.device import MODELS, TESTS_BY_COMPONENT
from repairlab.fleet import Fleet

LAB_PER_MODEL = 2
LAB_SEED = 7
CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def _planned_devices() -> list[tuple[str, str]]:
    """(serial, model) pairs the session lab will contain. Serials are deterministic,
    so tests can be parametrized per device before the lab starts."""
    lab = Lab(per_model=LAB_PER_MODEL, seed=LAB_SEED)
    return [(d.serial, d.model) for d in lab.devices]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "device_test" in metafunc.fixturenames:
        cases = [
            (serial, test_id)
            for serial, model in _planned_devices()
            for comp in MODELS[model]["components"]
            for test_id in TESTS_BY_COMPONENT[comp]
        ]
        metafunc.parametrize("device_test", cases, ids=[f"{s}-{t}" for s, t in cases])
    elif "lab_serial" in metafunc.fixturenames:
        devices = _planned_devices()
        metafunc.parametrize("lab_serial", [s for s, _ in devices], ids=[s for s, _ in devices])


@pytest.fixture(scope="session")
def lab() -> Iterator[Lab]:
    with Lab(per_model=LAB_PER_MODEL, seed=LAB_SEED) as lab:
        if chaos.enabled():
            chaos.apply(lab)
        yield lab


@pytest.fixture(scope="session")
def fleet(lab: Lab) -> Fleet:
    return Fleet(lab.inventory(), client_timeout=1.0)


@pytest.fixture
def leased(fleet: Fleet, request: pytest.FixtureRequest) -> Callable[..., contextlib.AbstractContextManager[DiagClient]]:
    """Lease a device from the shared fleet and hand back a connected client."""

    @contextlib.contextmanager
    def _lease(**selector: str) -> Iterator[DiagClient]:
        with fleet.lease(request.node.nodeid, timeout=3.0, **selector) as rec:
            with DiagClient(rec.host, rec.port, timeout=1.0) as client:
                yield client

    return _lease


@pytest.fixture
def fresh_device() -> Iterator[Callable[[str], DiagClient]]:
    """A brand-new single-device lab, for tests that mutate device state (repairs, faults)."""
    labs: list[Lab] = []
    clients: list[DiagClient] = []

    def _make(model: str) -> DiagClient:
        lab = Lab(models=[model], per_model=1, seed=99).start()
        labs.append(lab)
        rec = lab.inventory()[0]
        client = DiagClient(rec.host, rec.port, timeout=1.0).connect()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.close()
    for lab in labs:
        lab.stop()
