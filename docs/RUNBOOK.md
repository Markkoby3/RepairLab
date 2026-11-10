# Lab runbook

## A nightly run is red

1. Open the `chaos-triage` or `test-results-*` artifact and read `triage.md` top to bottom.
   Clusters are ordered product → test → unknown → infra → flaky.
2. **product, multi-device:** treat as a software regression. Find the first nightly where
   the cluster signature appeared and bisect the changes between it and the last green run.
3. **product, single-device:** treat as hardware until proven otherwise. Rerun the cluster's
   tests on a sibling unit of the same model:
   `pytest -k "DVL107000 and battery"`. If the sibling passes, pull the unit for bench diagnosis.
4. **test:** the test or fixture is broken. Fix it before rerunning anything else; it is
   hiding real signal.
5. **infra:** do not file a product bug. Go to "A device is quarantined".
6. **flaky:** log the test id and device. If the same test flakes on three nightlies in a
   week, open a stabilization ticket.

## A device is quarantined

A device is quarantined after `quarantine_after` consecutive failed health checks
(default 2). Tests skip it automatically; a test pinned to it fails with `LeaseTimeout`.

1. Check `Fleet.table()` for `LAST ERROR`:
   * `DeviceTimeout` → the device is up but not answering. Try `fleet.recover(serial)`,
     which reboots it and re-runs the health check.
   * `DeviceDisconnected` → the link is down. A remote reboot cannot reach it. Check cable,
     hub port, and power, then run `fleet.recover(serial)`.
   * `serial mismatch` → the wrong device is on that port. Fix the inventory or recable.
2. If recovery fails twice, mark the unit out of service and file a lab ticket with the
   device history (`fleet.status(serial).history`).

## Adding a diagnostic

1. Add the test id to `TESTS_BY_COMPONENT` and its check to `SimDevice._evaluate`.
2. Add its spec limit to `SPEC` in `tests/test_diagnostics.py`. The per-device
   parametrization picks it up on every unit automatically.
3. If the response shape changes, update `contracts/protocol/` and the Swift models in the
   same PR. Both suites will fail until they agree.
