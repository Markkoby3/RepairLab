"""Lab infrastructure: leasing, health checks, quarantine, and recovery."""

from __future__ import annotations

import threading
import time

import pytest

from repairlab.agent import Lab
from repairlab.client import DiagClient
from repairlab.fleet import Fleet, LeaseTimeout


@pytest.fixture
def small_lab():
    with Lab(models=["DV-P1", "DV-T1"], per_model=2, seed=11) as lab:
        yield lab


def _inject(rec, **fault):
    with DiagClient(rec.host, rec.port) as c:
        c.call("inject_fault", **fault)


def test_lease_is_exclusive_under_concurrency(small_lab):
    fleet = Fleet(small_lab.inventory())
    holders: dict[str, list[str]] = {}
    overlaps: list[str] = []
    lock = threading.Lock()

    def worker(name: str) -> None:
        with fleet.lease(name, model="DV-P1", timeout=10) as rec:
            with lock:
                holders.setdefault(rec.serial, []).append(name)
                if len(holders[rec.serial]) > 1:
                    overlaps.append(rec.serial)
            time.sleep(0.02)
            with lock:
                holders[rec.serial].remove(name)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not overlaps, f"devices double-leased: {overlaps}"
    assert fleet.summary() == {"available": 4, "leased": 0, "quarantined": 0}


def test_lease_waits_then_times_out_when_pool_busy(small_lab):
    fleet = Fleet(small_lab.inventory())
    a = fleet.acquire("a", model="DV-T1")
    b = fleet.acquire("b", model="DV-T1")
    start = time.monotonic()
    with pytest.raises(LeaseTimeout, match="timed out"):
        fleet.acquire("c", model="DV-T1", timeout=0.3)
    assert time.monotonic() - start >= 0.3
    fleet.release(a.serial)
    assert fleet.acquire("c", model="DV-T1", timeout=0.3).serial == a.serial
    fleet.release(b.serial)


def test_released_device_wakes_waiting_test(small_lab):
    fleet = Fleet(small_lab.inventory())
    first = fleet.acquire("a", model="DV-T1")
    fleet.acquire("b", model="DV-T1")
    threading.Timer(0.1, fleet.release, args=(first.serial,)).start()
    assert fleet.acquire("c", model="DV-T1", timeout=2).serial == first.serial


def test_least_used_device_is_preferred(small_lab):
    fleet = Fleet(small_lab.inventory())
    seen = []
    for i in range(4):
        with fleet.lease(f"t{i}", model="DV-P1") as rec:
            seen.append(rec.serial)
    assert seen[0] != seen[1] and seen[0] == seen[2] and seen[1] == seen[3]


def test_dropped_link_device_is_quarantined_and_skipped(small_lab):
    fleet = Fleet(small_lab.inventory(), quarantine_after=1)
    bad, good = [r for r in small_lab.inventory() if r.model == "DV-P1"]
    _inject(bad, kind="drop_connection")

    for i in range(3):
        with fleet.lease(f"t{i}", model="DV-P1") as rec:
            assert rec.serial == good.serial
    st = fleet.status(bad.serial)
    assert st.state == "quarantined"
    assert "DeviceDisconnected" in (st.last_error or "")


def test_no_healthy_device_raises_immediately(small_lab):
    fleet = Fleet(small_lab.inventory(), quarantine_after=1)
    for rec in [r for r in small_lab.inventory() if r.model == "DV-T1"]:
        _inject(rec, kind="drop_connection")
        fleet.health_check(rec.serial)
    start = time.monotonic()
    with pytest.raises(LeaseTimeout, match="No healthy device"):
        fleet.acquire("t", model="DV-T1", timeout=5)
    assert time.monotonic() - start < 1.0


def test_hung_device_recovers_after_reboot(small_lab):
    fleet = Fleet(small_lab.inventory(), quarantine_after=1, client_timeout=0.3)
    rec = small_lab.inventory()[0]
    _inject(rec, kind="hung")

    assert fleet.health_check(rec.serial) is False
    assert fleet.status(rec.serial).state == "quarantined"
    assert "DeviceTimeout" in fleet.status(rec.serial).last_error

    assert fleet.recover(rec.serial) is True
    st = fleet.status(rec.serial)
    assert st.state == "available" and st.history[-1] == "recovered"


def test_unrecoverable_device_stays_quarantined(small_lab):
    fleet = Fleet(small_lab.inventory(), quarantine_after=1, client_timeout=0.3)
    rec = small_lab.inventory()[1]
    _inject(rec, kind="drop_connection")
    fleet.health_check(rec.serial)
    assert fleet.recover(rec.serial) is False
    assert fleet.status(rec.serial).state == "quarantined"


def test_wrong_device_on_port_fails_health_check(small_lab):
    inv = small_lab.inventory()
    swapped = [type(inv[0])(serial=inv[0].serial, model=inv[0].model, host=inv[1].host, port=inv[1].port)]
    fleet = Fleet(swapped, quarantine_after=1)
    assert fleet.health_check(inv[0].serial) is False
    assert "serial mismatch" in fleet.status(inv[0].serial).last_error


def test_status_table_lists_every_device(small_lab):
    fleet = Fleet(small_lab.inventory())
    table = fleet.table()
    for rec in small_lab.inventory():
        assert rec.serial in table
