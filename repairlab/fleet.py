"""Device fleet management for the automation lab.

Responsibilities that a real QA lab needs and that tests should never re-implement:
* leasing: one test owns a device at a time; others wait or time out
* health checks: a device must answer `hello` and `info` before it is handed out
* quarantine: devices that fail health checks repeatedly are pulled from the pool
* recovery: reboot a quarantined device and return it to service if it comes back
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

from .agent import DeviceRecord
from .client import DeviceTransportError, DiagClient


class LeaseTimeout(Exception):
    """No healthy device matching the request became available in time."""


@dataclass
class DeviceStatus:
    record: DeviceRecord
    state: str = "available"  # available | leased | quarantined
    holder: str | None = None
    consecutive_failures: int = 0
    leases: int = 0
    last_error: str | None = None
    history: list[str] = field(default_factory=list)


class Fleet:
    def __init__(self, inventory: list[DeviceRecord], quarantine_after: int = 2, client_timeout: float = 1.0):
        self._status = {r.serial: DeviceStatus(r) for r in inventory}
        self._cond = threading.Condition()
        self.quarantine_after = quarantine_after
        self.client_timeout = client_timeout

    # ----------------------------------------------------------------- health
    def health_check(self, serial: str) -> bool:
        st = self._status[serial]
        try:
            with DiagClient(st.record.host, st.record.port, timeout=self.client_timeout) as c:
                if c.hello()["serial"] != serial:
                    raise DeviceTransportError("serial mismatch: wrong device on this port")
                c.info()
        except DeviceTransportError as e:
            with self._cond:
                st.consecutive_failures += 1
                st.last_error = f"{type(e).__name__}: {e}"
                st.history.append(f"health_fail:{type(e).__name__}")
                if st.consecutive_failures >= self.quarantine_after and st.state != "quarantined":
                    st.state = "quarantined"
                    st.history.append("quarantined")
                    self._cond.notify_all()
            return False
        with self._cond:
            st.consecutive_failures = 0
            st.last_error = None
        return True

    # ----------------------------------------------------------------- leasing
    def acquire(self, holder: str, model: str | None = None, tag: str | None = None,
                timeout: float = 5.0, serial: str | None = None) -> DeviceRecord:
        deadline = time.monotonic() + timeout
        while True:
            with self._cond:
                candidate = self._pick(model, tag, serial)
                if candidate is None and not self._any_possible(model, tag, serial):
                    raise LeaseTimeout(f"No healthy device in fleet for model={model} tag={tag} serial={serial}")
                if candidate is not None:
                    candidate.state, candidate.holder = "leased", holder
                    candidate.leases += 1
                    candidate.history.append(f"leased:{holder}")
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LeaseTimeout(f"timed out after {timeout}s waiting for model={model} tag={tag}")
                    self._cond.wait(remaining)
                    continue
            # Health check outside the lock; a sick device goes back and we try another.
            if self.health_check(candidate.record.serial):
                return candidate.record
            self.release(candidate.record.serial)

    def release(self, serial: str) -> None:
        with self._cond:
            st = self._status[serial]
            if st.state == "leased":
                st.state, st.holder = "available", None
                st.history.append("released")
            self._cond.notify_all()

    @contextlib.contextmanager
    def lease(self, holder: str, model: str | None = None, tag: str | None = None,
              timeout: float = 5.0, serial: str | None = None) -> Iterator[DeviceRecord]:
        record = self.acquire(holder, model=model, tag=tag, timeout=timeout, serial=serial)
        try:
            yield record
        finally:
            self.release(record.serial)

    @staticmethod
    def _matches(st: DeviceStatus, model: str | None, tag: str | None, serial: str | None) -> bool:
        return ((model is None or st.record.model == model)
                and (tag is None or tag in st.record.tags)
                and (serial is None or st.record.serial == serial))

    def _pick(self, model: str | None, tag: str | None, serial: str | None) -> DeviceStatus | None:
        # Least-used first spreads wear across the fleet.
        free = [s for s in self._status.values()
                if s.state == "available" and self._matches(s, model, tag, serial)]
        return min(free, key=lambda s: s.leases) if free else None

    def _any_possible(self, model: str | None, tag: str | None, serial: str | None) -> bool:
        return any(s.state != "quarantined" and self._matches(s, model, tag, serial)
                   for s in self._status.values())

    # ---------------------------------------------------------------- recovery
    def recover(self, serial: str) -> bool:
        """Reboot a quarantined device; return it to the pool if it is healthy again."""
        st = self._status[serial]
        try:
            with DiagClient(st.record.host, st.record.port, timeout=self.client_timeout) as c:
                c.reboot()
        except DeviceTransportError as e:
            st.history.append(f"recover_fail:{type(e).__name__}")
            return False
        if self.health_check(serial):
            with self._cond:
                st.state = "available"
                st.history.append("recovered")
                self._cond.notify_all()
            return True
        return False

    # ------------------------------------------------------------------ views
    def status(self, serial: str) -> DeviceStatus:
        return self._status[serial]

    def summary(self) -> dict[str, int]:
        counts = {"available": 0, "leased": 0, "quarantined": 0}
        for s in self._status.values():
            counts[s.state] += 1
        return counts

    def table(self) -> str:
        rows = [f"{'SERIAL':<14}{'MODEL':<8}{'STATE':<13}{'LEASES':>6}  LAST ERROR"]
        for s in self._status.values():
            rows.append(f"{s.record.serial:<14}{s.record.model:<8}{s.state:<13}{s.leases:>6}  {s.last_error or '-'}")
        return "\n".join(rows)
