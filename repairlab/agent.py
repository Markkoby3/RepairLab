"""Device agents: each SimDevice is served over TCP with a JSON-lines protocol.

Request:  {"id": 1, "method": "run_test", "params": {"test_id": "battery.health"}}
Response: {"id": 1, "result": {...}}  or  {"id": 1, "error": {"code": "...", "message": "..."}}

A Lab runs many agents on one asyncio loop in a background thread, so synchronous
pytest code can drive a whole fleet of devices on localhost.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict, dataclass
from typing import Any

from .device import MODELS, DeviceError, Fault, SimDevice

PROTOCOL_VERSION = "1.2"


def handle(device: SimDevice, method: str, params: dict[str, Any]) -> Any:
    """Dispatch one protocol method against a device. Raises DeviceError."""
    if method == "hello":
        return {"protocol": PROTOCOL_VERSION, "serial": device.serial}
    if method == "info":
        return device.info()
    if method == "list_tests":
        return {"tests": device.list_tests()}
    if method == "run_test":
        return device.run_test(_param(params, "test_id"))
    if method == "enter_repair_mode":
        device.enter_repair_mode()
        return {"repair_mode": True}
    if method == "exit_repair_mode":
        device.exit_repair_mode()
        return {"repair_mode": False}
    if method == "replace_part":
        return device.replace_part(_param(params, "component"), _param(params, "part_serial"))
    if method == "pair":
        return device.pair(_param(params, "component"))
    if method == "calibrate":
        return device.calibrate(_param(params, "component"))
    if method == "reboot":
        return device.reboot()
    if method == "inject_fault":
        device.inject_fault(Fault(**params))
        return {"faults": len(device.faults)}
    if method == "clear_faults":
        device.clear_faults()
        return {"faults": 0}
    raise DeviceError("UNKNOWN_METHOD", f"no such method: {method}")


def _peek_method(line: bytes) -> str | None:
    try:
        return json.loads(line).get("method")
    except (json.JSONDecodeError, AttributeError):
        return None


def _param(params: dict[str, Any], key: str) -> Any:
    if key not in params:
        raise DeviceError("MISSING_PARAM", f"missing required param '{key}'")
    return params[key]


class DeviceAgent:
    def __init__(self, device: SimDevice):
        self.device = device
        self.server: asyncio.base_events.Server | None = None
        self.port: int = 0
        self.requests_served = 0
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self, host: str = "127.0.0.1") -> None:
        self.server = await asyncio.start_server(self._client, host, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            for w in list(self._writers):
                w.transport.abort()
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=2)
            except asyncio.TimeoutError:
                pass

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while line := await reader.readline():
                if self.device.should_drop():
                    break  # simulate a USB/network link dropping mid-session
                if delay := self.device.latency_ms():
                    await asyncio.sleep(delay / 1000)
                if self.device.is_hung() and _peek_method(line) != "reboot":
                    await asyncio.sleep(30)  # firmware hang: only a reboot gets through
                    continue
                writer.write((json.dumps(self._respond(line)) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()

    def _respond(self, line: bytes) -> dict[str, Any]:
        self.requests_served += 1
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            return {"id": None, "error": {"code": "BAD_REQUEST", "message": "invalid JSON"}}
        if not isinstance(req, dict) or "method" not in req:
            return {"id": None, "error": {"code": "BAD_REQUEST", "message": "missing method"}}
        req_id = req.get("id")
        try:
            return {"id": req_id, "result": handle(self.device, req["method"], req.get("params") or {})}
        except DeviceError as e:
            return {"id": req_id, "error": {"code": e.code, "message": e.message}}
        except TypeError as e:
            return {"id": req_id, "error": {"code": "BAD_PARAMS", "message": str(e)}}


@dataclass
class DeviceRecord:
    serial: str
    model: str
    host: str
    port: int
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Lab:
    """A set of simulated devices served on localhost from one background loop."""

    def __init__(self, models: list[str] | None = None, per_model: int = 2, seed: int = 7):
        models = models or list(MODELS)
        self.devices: list[SimDevice] = []
        for m in models:
            for i in range(per_model):
                self.devices.append(SimDevice(serial=f"{m.replace('-', '')}{seed:02d}{i:03d}", model=m, seed=seed + i))
        self.agents: list[DeviceAgent] = [DeviceAgent(d) for d in self.devices]
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="lab-loop")

    def start(self) -> "Lab":
        self._thread.start()
        for a in self.agents:
            asyncio.run_coroutine_threadsafe(a.start(), self._loop).result(timeout=5)
        return self

    def stop(self) -> None:
        for a in self.agents:
            asyncio.run_coroutine_threadsafe(a.stop(), self._loop).result(timeout=5)
        asyncio.run_coroutine_threadsafe(self._cancel_pending(), self._loop).result(timeout=5)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    @staticmethod
    async def _cancel_pending() -> None:
        current = asyncio.current_task()
        tasks = [t for t in asyncio.all_tasks() if t is not current]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def inventory(self) -> list[DeviceRecord]:
        return [
            DeviceRecord(serial=a.device.serial, model=a.device.model, host="127.0.0.1", port=a.port,
                         tags=(MODELS[a.device.model]["family"],))
            for a in self.agents
        ]

    def device(self, serial: str) -> SimDevice:
        return next(d for d in self.devices if d.serial == serial)

    def __enter__(self) -> "Lab":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
