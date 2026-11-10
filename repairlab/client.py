"""Synchronous client for the device diagnostic protocol.

Errors are split into two families on purpose, because failure triage depends on it:

* DeviceCommandError – the device answered and rejected the command (product/test signal)
* DeviceTransportError and subclasses – we could not talk to the device (infra signal)
"""

from __future__ import annotations

import itertools
import json
import socket
from typing import Any


class DeviceTransportError(Exception):
    """Could not communicate with the device (infrastructure problem)."""


class DeviceTimeout(DeviceTransportError):
    pass


class DeviceDisconnected(DeviceTransportError):
    pass


class DeviceCommandError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class DiagClient:
    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self._sock: socket.socket | None = None
        self._file: Any = None
        self._ids = itertools.count(1)

    # ------------------------------------------------------------ connection
    def connect(self) -> "DiagClient":
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            raise DeviceDisconnected(f"cannot connect to {self.host}:{self.port}: {e}") from e
        self._file = self._sock.makefile("rwb")
        return self

    def close(self) -> None:
        for closer in (self._file, self._sock):
            try:
                if closer:
                    closer.close()
            except OSError:
                pass
        self._sock = self._file = None

    def __enter__(self) -> "DiagClient":
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- calls
    def call(self, method: str, **params: Any) -> Any:
        if self._file is None:
            self.connect()
        req_id = next(self._ids)
        payload = json.dumps({"id": req_id, "method": method, "params": params}).encode() + b"\n"
        try:
            self._file.write(payload)
            self._file.flush()
            line = self._file.readline()
        except socket.timeout as e:
            self.close()
            raise DeviceTimeout(f"{method} timed out after {self.timeout}s") from e
        except OSError as e:
            self.close()
            raise DeviceDisconnected(f"{method}: link error: {e}") from e
        if not line:
            self.close()
            raise DeviceDisconnected(f"{method}: device closed the connection")
        resp = json.loads(line)
        if resp.get("id") != req_id:
            raise DeviceTransportError(f"{method}: response id {resp.get('id')} != request id {req_id}")
        if "error" in resp:
            raise DeviceCommandError(resp["error"]["code"], resp["error"]["message"])
        return resp["result"]

    # Convenience wrappers keep tests readable.
    def hello(self) -> dict[str, Any]:
        return self.call("hello")

    def info(self) -> dict[str, Any]:
        return self.call("info")

    def list_tests(self) -> list[str]:
        return self.call("list_tests")["tests"]

    def run_test(self, test_id: str) -> dict[str, Any]:
        return self.call("run_test", test_id=test_id)

    def reboot(self) -> dict[str, Any]:
        return self.call("reboot")
