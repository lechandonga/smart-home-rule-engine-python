"""Device service: registration, state reporting, command dispatch.

Determinism rules
-----------------
All state mutation happens inside a single ``store.mutate`` critical section,
so concurrent reports and commands are applied in one well-defined total
(lock-acquisition) order; the returned device ``version`` makes the resulting
state explainable.

* **Duplicate report** (same device_id + token): the original result is echoed,
  no state change, no rule re-trigger.
* **Stale report** (seq <= last accepted seq): rejected with ``stale_state``.
* **Invalid state** (unknown/out-of-range property): rejected with
  ``validation_failed`` before any state is touched.
* **Duplicate command** (same command_id): the original result is echoed with
  ``duplicate=True``.
* **Command to an offline device**: rejected with ``device_offline``.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from app.audit import AuditLog
from app.auth import Caller, CallerRegistry, can_access_device
from app.errors import (
    Conflict,
    DeviceOffline,
    DuplicateCommand,
    Forbidden,
    NotFound,
    StaleReport,
)
from app.models import (
    CommandRecord,
    CommandRequest,
    CommandResult,
    Device,
    DeviceStatus,
    DeviceType,
    RegisterDeviceRequest,
    ReportResult,
    RuleExecution,
    StateReport,
)
from app.store import State, Store
from app.util import new_id, now_iso, validate_state

log = logging.getLogger("smart_home.devices")


class DeviceService:
    def __init__(
        self,
        store: Store,
        registry: CallerRegistry,
        audit: AuditLog,
    ) -> None:
        self.store = store
        self.registry = registry
        self.audit = audit
        # The rule engine hook is injected after construction to break the
        # service <-> engine construction cycle.
        self.on_state_change: Callable[..., list[str]] | None = None

    # ------------------------------------------------------------ helpers

    def _audit(
        self,
        caller: Caller,
        action: str,
        resource_type: str,
        resource_id: str,
        allowed: bool,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        from app.models import AuditEntry

        self.audit.append(
            AuditEntry(
                audit_id=new_id("aud"),
                ts=now_iso(),
                caller_id=caller.caller_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                result="allowed" if allowed else "denied",
                reason=reason,
                details=details or {},
            )
        )

    def _require_device_snapshot(self, device_id: str) -> Device:
        dev = self.store.snapshot().devices.get(device_id)
        if dev is None:
            raise NotFound(f"设备不存在: {device_id}")
        return dev

    def _require_device_access(self, caller: Caller, device_id: str, action: str) -> Device:
        dev = self._require_device_snapshot(device_id)
        if not can_access_device(caller, dev):
            self._audit(
                caller, action, "device", device_id, False,
                reason="不在授权设备范围内",
                details={"allowed_devices": caller.allowed_devices},
            )
            raise Forbidden(
                f"调用方 {caller.caller_id} 无权操作设备 {device_id}",
                details={"allowed_devices": caller.allowed_devices},
            )
        return dev

    # -------------------------------------------------------------- reads

    def list_devices(self, caller: Caller) -> list[Device]:
        snap = self.store.snapshot()
        devices = [d for d in snap.devices.values() if can_access_device(caller, d)]
        self._audit(caller, "list_devices", "device", "*", True,
                    details={"matched": len(devices)})
        return devices

    def get_device(self, device_id: str, caller: Caller) -> Device:
        self._require_device_access(caller, device_id, "get_device")
        self._audit(caller, "get_device", "device", device_id, True)
        return self._require_device_snapshot(device_id)

    def set_status(
        self, device_id: str, status: DeviceStatus, caller: Caller
    ) -> Device:
        self._require_device_access(caller, device_id, "set_status")

        def _do(st: State) -> Device:
            dev = st.devices[device_id]
            dev.status = status
            dev.updated_at = now_iso()
            result = dev.model_copy(deep=True)
            log.info(
                "set_status caller=%s device=%s status=%s version=%d",
                caller.caller_id, device_id, status.value, dev.version,
            )
            return result

        result = self.store.mutate(_do)
        self._audit(caller, "set_status", "device", device_id, True,
                    details={"status": status.value})
        return result

    # ------------------------------------------------------------ register

    def register_device(self, req: RegisterDeviceRequest, caller: Caller) -> Device:
        if caller.role != "admin" and req.device_id not in caller.allowed_devices:
            self._audit(
                caller, "register_device", "device", req.device_id, False,
                reason="非管理员且设备不在其授权范围内",
                details={"allowed_devices": caller.allowed_devices},
            )
            raise Forbidden(
                f"调用方 {caller.caller_id} 无权注册设备 {req.device_id}"
            )
        validate_state(req.type, req.initial_state)

        def _do(st: State) -> Device:
            if req.device_id in st.devices:
                raise Conflict(
                    f"设备已存在: {req.device_id}",
                    details={"device_id": req.device_id},
                )
            dev = Device(
                device_id=req.device_id,
                name=req.name,
                type=req.type,
                owner=caller.caller_id,
                status=DeviceStatus.ONLINE,
                state=dict(req.initial_state),
                version=0,
                updated_at=now_iso(),
            )
            st.devices[req.device_id] = dev
            log.info(
                "register_device caller=%s device=%s type=%s state=%s",
                caller.caller_id, req.device_id, req.type.value, dev.state,
            )
            return dev.model_copy(deep=True)

        dev = self.store.mutate(_do)
        self._audit(caller, "register_device", "device", req.device_id, True,
                    details={"type": req.type.value})
        return dev

    # ------------------------------------------------------------- reports

    def report_state(self, report: StateReport, caller: Caller) -> ReportResult:
        device = self._require_device_access(caller, report.device_id, "report_state")
        # Validate before entering the mutation so invalid data can never be
        # partially accepted.
        validate_state(device.type, report.state)

        log.info(
            "report_state input caller=%s device=%s seq=%d token=%s state=%s",
            caller.caller_id, report.device_id, report.seq, report.token, report.state,
        )

        def _do(st: State) -> ReportResult:
            dev = st.devices[report.device_id]
            token_key = report.token

            # 1) Exact token replay -> deterministic echo, never re-triggers.
            seen = st.report_tokens.get(report.device_id, {}).get(token_key)
            if seen is not None:
                log.info(
                    "report duplicate device=%s token=%s -> echo seq=%d execution_ids=%s",
                    report.device_id, token_key, seen["seq"], seen["execution_ids"],
                )
                return ReportResult(
                    accepted=True,
                    reason="duplicate",
                    device=dev.model_copy(deep=True),
                    rule_execution_ids=list(seen["execution_ids"]),
                )

            # 2) Sequence ordering: equal seq with a different token is a
            #    conflicting duplicate -> rejected; lower seq is stale.
            if dev.last_report_seq is not None and report.seq < dev.last_report_seq:
                log.info(
                    "report stale device=%s incoming_seq=%d last_seq=%d -> reject",
                    report.device_id, report.seq, dev.last_report_seq,
                )
                raise StaleReport(
                    f"过期上报：seq={report.seq} <= 已接受 seq={dev.last_report_seq}",
                    details={
                        "incoming_seq": report.seq,
                        "last_accepted_seq": dev.last_report_seq,
                    },
                )
            if dev.last_report_seq is not None and report.seq == dev.last_report_seq:
                raise StaleReport(
                    f"seq={report.seq} 已被另一上报占用（token 冲突）",
                    details={
                        "incoming_seq": report.seq,
                        "accepted_token": dev.last_report_token,
                    },
                )

            # 3) Accept: merge state, bump version, remember token.
            prev_state = dict(dev.state)
            dev.state = {**dev.state, **report.state}
            dev.last_report_seq = report.seq
            dev.last_report_token = token_key
            dev.version += 1
            dev.updated_at = now_iso()
            st.report_tokens.setdefault(report.device_id, {})[token_key] = {
                "seq": report.seq,
                "execution_ids": [],
            }

            execution_ids: list[str] = []
            if self.on_state_change is not None and prev_state != dev.state:
                execution_ids = self.on_state_change(
                    st,
                    device_id=dev.device_id,
                    seq=report.seq,
                    state=dict(dev.state),
                    prev_state=prev_state,
                )
                st.report_tokens[report.device_id][token_key]["execution_ids"] = list(
                    execution_ids
                )

            log.info(
                "report accepted device=%s seq=%d new_version=%d state=%s executions=%s",
                report.device_id, report.seq, dev.version, dev.state, execution_ids,
            )
            return ReportResult(
                accepted=True,
                device=dev.model_copy(deep=True),
                rule_execution_ids=execution_ids,
            )

        try:
            result = self.store.mutate(_do)
        except StaleReport as exc:
            self._audit(
                caller, "report_state", "device", report.device_id, False,
                reason=exc.code,
                details={"seq": report.seq, "token": report.token,
                         "last_accepted_seq": exc.details.get("last_accepted_seq")},
            )
            raise
        self._audit(
            caller, "report_state", "device", report.device_id, True,
            details={
                "seq": report.seq,
                "token": report.token,
                "accepted": result.accepted,
                "reason": result.reason,
                "version": result.device.version,
                "executions": result.rule_execution_ids,
            },
        )
        return result

    # ------------------------------------------------------------ commands

    def send_command(
        self,
        req: CommandRequest,
        caller: Caller,
        source_rule_id: str | None = None,
        st: State | None = None,
    ) -> CommandResult:
        """Dispatch a command.

        When ``st`` is supplied (rule-engine context) the command runs inside
        the caller's existing locked mutation. Otherwise it opens its own.
        """
        if st is None:
            self._require_device_access(caller, req.device_id, "send_command")
            result = self.store.mutate(lambda state: self._send_command(req, caller, None, state))
            if (
                not result.duplicate
                and result.command.status == "rejected"
                and result.command.reason == "device_offline"
            ):
                self._audit(
                    caller, "send_command", "device", req.device_id, False,
                    reason="device_offline",
                    details={"command_id": req.command_id, "params": req.params},
                )
                raise DeviceOffline(
                    f"设备离线，命令被拒绝: {req.device_id}",
                    details={"command_id": req.command_id},
                )
            return result
        return self._send_command(req, caller, source_rule_id, st)

    def _send_command(
        self,
        req: CommandRequest,
        caller: Caller,
        source_rule_id: str | None,
        st: State,
    ) -> CommandResult:
        dev = st.devices.get(req.device_id)
        if dev is None:
            raise NotFound(f"设备不存在: {req.device_id}")

        # Idempotency: same command id -> echo the recorded verdict.
        prior = st.commands.get(req.command_id)
        if prior is not None:
            log.info(
                "command duplicate id=%s device=%s recorded_status=%s -> echo",
                req.command_id, req.device_id, prior.status,
            )
            return CommandResult(duplicate=True, command=prior.model_copy(deep=True))

        validate_state(dev.type, req.params)

        # Simulated delivery: offline devices refuse commands deterministically.
        if dev.status == DeviceStatus.OFFLINE:
            record = CommandRecord(
                command_id=req.command_id,
                device_id=req.device_id,
                params=dict(req.params),
                status="rejected",
                reason="device_offline",
                caller_id=caller.caller_id,
                source_rule_id=source_rule_id,
                executed_at=now_iso(),
            )
            st.commands[req.command_id] = record
            log.info(
                "command rejected id=%s device=%s reason=device_offline",
                req.command_id, req.device_id,
            )
            return CommandResult(duplicate=False, command=record.model_copy(deep=True))

        # Apply: the simulated device adopts commanded state immediately.
        prev_state = dict(dev.state)
        dev.state = {**dev.state, **req.params}
        dev.version += 1
        dev.updated_at = record_ts = now_iso()
        record = CommandRecord(
            command_id=req.command_id,
            device_id=req.device_id,
            params=dict(req.params),
            status="applied",
            reason=None,
            caller_id=caller.caller_id,
            source_rule_id=source_rule_id,
            executed_at=record_ts,
        )
        st.commands[req.command_id] = record
        log.info(
            "command applied id=%s device=%s params=%s new_version=%d source_rule=%s",
            req.command_id, req.device_id, req.params, dev.version, source_rule_id,
        )

        # A command is itself a state change -> rules may react to it. Command
        # events use a distinct key namespace ("cmd:<command_id>") inside the
        # engine; duplicate command ids are already echoed above, so a command
        # event can never fire twice.
        if self.on_state_change is not None and prev_state != dev.state:
            self.on_state_change(
                st,
                device_id=dev.device_id,
                seq=(dev.last_report_seq or 0),
                state=dict(dev.state),
                prev_state=prev_state,
                caused_by_command=req.command_id,
            )

        result = CommandResult(duplicate=False, command=record.model_copy(deep=True))

        # Audit only top-level API commands (engine commands are traced via
        # rule execution records to avoid double auditing).
        if source_rule_id is None:
            self._audit(
                caller, "send_command", "device", req.device_id, True,
                details={"command_id": req.command_id, "params": req.params,
                         "status": "applied", "version": dev.version},
            )
        return result
