"""Device semantics: concurrent reports, duplicates, stale, offline, invalid."""

from __future__ import annotations

import logging

from app.errors import DeviceOffline, StaleReport
from app.models import (
    CommandRequest,
    DeviceStatus,
    RegisterDeviceRequest,
    StateReport,
)
from tests.conftest import log_verdict, run_concurrently


def _register(container, caller, device_id="light-living", dtype="light", state=None):
    return container.device_service.register_device(
        RegisterDeviceRequest(
            device_id=device_id, name=device_id, type=dtype,
            initial_state=state if state is not None else {"power": False},
        ),
        caller,
    )


def test_concurrent_reports_only_highest_seq_accepted(container, admin, caplog):
    _register(container, admin)
    # 20 threads each send a different seq (0..19) with distinct tokens at once.
    n = 20

    def task(i):
        try:
            return container.device_service.report_state(
                StateReport(device_id="light-living",
                            state={"brightness": i}, seq=i, token=f"tok-{i}"),
                admin,
            )
        except StaleReport as e:
            return ("stale", e.code)

    results = run_concurrently(task, n)
    accepted = [r for r in results if not isinstance(r, tuple)]
    stale = [r for r in results if isinstance(r, tuple)]

    dev = container.store.snapshot().devices["light-living"]
    # Exactly one winner: the maximum seq; state must match it; version == 1.
    assert len(accepted) == 1
    assert dev.last_report_seq == n - 1
    assert dev.state["brightness"] == n - 1
    assert dev.version == 1
    log_verdict(caplog, "concurrent_reports",
                {"threads": n, "seqs": list(range(n))},
                {"winner_seq": dev.last_report_seq, "state": dev.state,
                 "version": dev.version, "stale_count": len(stale)},
                "唯一胜出 seq=最大值 19；其余按 seq<19 判 stale；最终状态由胜出者决定")


def test_same_token_concurrent_is_single_acceptance(container, admin, caplog):
    _register(container, admin)
    n = 10

    def task(_i):
        r = container.device_service.report_state(
            StateReport(device_id="light-living", state={"brightness": 50},
                        seq=1, token="SAME"),
            admin,
        )
        return r.reason

    reasons = run_concurrently(task, n)
    dev = container.store.snapshot().devices["light-living"]
    assert reasons.count(None) == 1          # one real acceptance
    assert reasons.count("duplicate") == n - 1
    assert dev.version == 1
    log_verdict(caplog, "concurrent_same_token",
                {"threads": n, "seq": 1, "token": "SAME"},
                {"version": dev.version, "reasons": reasons},
                "同 token 只有一次真正生效，其余全部 duplicate 回显")


def test_duplicate_command_echoes_same_verdict(container, admin, caplog):
    _register(container, admin)
    req = CommandRequest(command_id="cmd-1", device_id="light-living",
                         params={"power": True})
    n = 8
    results = run_concurrently(lambda _i: container.device_service.send_command(req, admin), n)

    flags = sorted(r.duplicate for r in results)
    assert flags == [False] + [True] * (n - 1)
    statuses = {r.command.status for r in results}
    assert statuses == {"applied"}
    # Exactly one applied command record, one state version bump.
    snap = container.store.snapshot()
    assert len(snap.commands) == 1
    assert snap.devices["light-living"].version == 1
    log_verdict(caplog, "duplicate_command",
                {"threads": n, "command_id": "cmd-1"},
                {"records": len(snap.commands), "version": snap.devices["light-living"].version},
                "同 command_id 只执行一次，其余回显 duplicate=true 的同一裁决")


def test_offline_device_commands_rejected_and_recorded(container, admin, caplog):
    _register(container, admin)
    container.device_service.set_status("light-living", DeviceStatus.OFFLINE, admin)

    raised = False
    try:
        container.device_service.send_command(
            CommandRequest(command_id="c-off", device_id="light-living",
                           params={"power": True}),
            admin,
        )
    except DeviceOffline as e:
        raised = True
        code = e.code
    assert raised and code == "device_offline"

    snap = container.store.snapshot()
    rec = snap.commands["c-off"]
    # Rejected commands are still traceable and remain idempotent on retry.
    assert rec.status == "rejected" and rec.reason == "device_offline"
    again = container.device_service.send_command(
        CommandRequest(command_id="c-off", device_id="light-living",
                       params={"power": True}),
        admin,
    )
    assert again.duplicate is True and again.command.status == "rejected"
    denied_audits = [a for a in container.audit.list(50) if a.result == "denied"]
    log_verdict(caplog, "offline_command",
                {"command_id": "c-off", "device_status": "offline"},
                {"record": rec.model_dump(),
                 "denied_audit_count": len(denied_audits)},
                "离线设备命令确定性拒绝，留痕为 rejected/device_offline，重试幂等回显")


def test_invalid_state_is_rejected_before_mutation(container, admin, caplog):
    _register(container, admin)
    from app.errors import ValidationFailed

    bad_states = [
        {"brightness": 150},          # out of range
        {"unknown_prop": 1},         # unknown property
        {"power": "yes"},            # wrong type
    ]
    seq = 1
    for bad in bad_states:
        try:
            container.device_service.report_state(
                StateReport(device_id="light-living", state=bad,
                            seq=seq, token=f"bad-{seq}"),
                admin,
            )
            assert False, bad
        except ValidationFailed:
            pass
        seq += 1

    dev = container.store.snapshot().devices["light-living"]
    assert dev.state == {"power": False} and dev.version == 0
    assert dev.last_report_seq is None
    log_verdict(caplog, "invalid_state",
                {"bad_states": bad_states},
                {"state": dev.state, "version": dev.version, "last_seq": dev.last_report_seq},
                "非法状态在进入写入区前被 validation_failed 拒绝，状态与版本完全不变")
