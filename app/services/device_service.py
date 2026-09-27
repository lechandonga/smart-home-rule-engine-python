"""设备注册、状态上报、命令下发。

确定性语义：
- 状态上报：state_version 必须严格大于当前版本，否则按"过期数据"拒绝；
  属性不在设备白名单内按"非法状态"拒绝。所有上报在同一把锁内串行化，
  并发到达时最终结果只取决于版本号，与到达顺序无关。
- 命令下发：同一 (caller, idempotency_key) 重复提交返回首次执行结果，
  不重复执行；设备离线时命令确定性地被拒绝并记录原因；
  同一设备存在未完成的冲突命令（不同 action/params）时拒绝新命令。
"""
from __future__ import annotations

import logging
import uuid

from ..errors import ConflictError, NotFoundError, StaleDataError, ValidationFailedError
from ..models import (Command, CommandRequest, CommandStatus, Device,
                      DeviceStatus, StateReport, utcnow)
from ..storage import Storage

logger = logging.getLogger("smart_home.device")


class DeviceService:
    def __init__(self, storage: Storage):
        self.storage = storage

    # ------------------------------------------------------------------
    def register_device(self, device: Device, caller: str) -> Device:
        with self.storage.lock:
            existing = self.storage.data.devices.get(device.device_id)
            if existing is not None:
                # 重复注册是幂等的：返回已存在的设备，不覆盖状态
                logger.info("register %s: already exists, keep existing", device.device_id)
                return existing
            self.storage.data.devices[device.device_id] = device
            self.storage.save()
            logger.info("register %s: ok by %s", device.device_id, caller)
            return device

    def get_device(self, device_id: str) -> Device:
        device = self.storage.data.devices.get(device_id)
        if device is None:
            raise NotFoundError(f"设备不存在: {device_id}",
                                detail={"device_id": device_id})
        return device

    def list_devices(self) -> list[Device]:
        return list(self.storage.data.devices.values())

    def set_status(self, device_id: str, online: bool, caller: str) -> Device:
        with self.storage.lock:
            device = self.get_device(device_id)
            device.status = DeviceStatus.ONLINE if online else DeviceStatus.OFFLINE
            device.updated_at = utcnow()
            self.storage.save()
            logger.info("status %s -> %s by %s", device_id, device.status, caller)
            return device

    # ------------------------------------------------------------------
    def _validate_state(self, device: Device, state: dict) -> None:
        allowed = device.allowed_properties
        if allowed is None:
            return
        bad = {}
        for key, value in state.items():
            if key not in allowed:
                bad[key] = "未知属性"
            elif value not in allowed[key]:
                bad[key] = f"非法取值 {value!r}，允许 {allowed[key]}"
        if bad:
            raise ValidationFailedError(
                f"设备 {device.device_id} 的状态包含非法属性",
                detail={"device_id": device.device_id, "invalid": bad},
            )

    def report_state(self, report: StateReport, caller: str) -> Device:
        with self.storage.lock:
            device = self.get_device(report.device_id)
            if report.state_version <= device.state_version:
                raise StaleDataError(
                    f"过期上报: 版本 {report.state_version} <= 当前 "
                    f"{device.state_version}",
                    detail={"device_id": device.device_id,
                            "current_version": device.state_version,
                            "report_version": report.state_version},
                )
            self._validate_state(device, report.state)
            device.state.update(report.state)
            device.state_version = report.state_version
            device.updated_at = utcnow()
            self.storage.save()
            logger.info("report %s: version=%d state=%s by %s",
                        device.device_id, device.state_version,
                        report.state, caller)
            return device

    # ------------------------------------------------------------------
    def send_command(self, req: CommandRequest, caller: str) -> Command:
        with self.storage.lock:
            device = self.get_device(req.device_id)

            # 1) 幂等：同一调用方同一 idempotency_key 直接返回首次结果
            if req.idempotency_key:
                for cmd in self.storage.data.commands.values():
                    if (cmd.idempotency_key == req.idempotency_key
                            and cmd.caller == caller):
                        logger.info("command dup key=%s -> reuse %s",
                                    req.idempotency_key, cmd.command_id)
                        return cmd

            # 2) 离线设备：确定性拒绝
            if device.status == DeviceStatus.OFFLINE:
                cmd = self._new_command(req, caller, CommandStatus.REJECTED,
                                        reason="device_offline")
                self.storage.save()
                logger.info("command %s rejected: device %s offline",
                            cmd.command_id, device.device_id)
                return cmd

            # 3) 冲突命令：同设备已有未完成的、内容不同的命令
            for other in self.storage.data.commands.values():
                if (other.device_id == req.device_id
                        and other.status == CommandStatus.PENDING
                        and (other.action != req.action
                             or other.params != req.params)):
                    raise ConflictError(
                        f"设备 {req.device_id} 存在冲突的待执行命令 "
                        f"{other.command_id}",
                        detail={"pending_command_id": other.command_id,
                                "pending_action": other.action},
                    )

            # 4) 执行（本地模拟：set 动作直接落到设备状态）
            cmd = self._new_command(req, caller, CommandStatus.EXECUTED)
            if req.action == "set":
                self._validate_state(device, req.params)
                device.state.update(req.params)
                device.state_version += 1
                device.updated_at = utcnow()
            self.storage.save()
            logger.info("command %s executed on %s: %s %s",
                        cmd.command_id, device.device_id, req.action, req.params)
            return cmd

    def _new_command(self, req: CommandRequest, caller: str,
                     status: CommandStatus, reason: str = "") -> Command:
        cmd = Command(
            command_id=uuid.uuid4().hex,
            device_id=req.device_id,
            action=req.action,
            params=req.params,
            status=status,
            idempotency_key=req.idempotency_key,
            caller=caller,
            reason=reason,
            finished_at=utcnow(),
        )
        self.storage.data.commands[cmd.command_id] = cmd
        return cmd

    def get_command(self, command_id: str) -> Command:
        cmd = self.storage.data.commands.get(command_id)
        if cmd is None:
            raise NotFoundError(f"命令不存在: {command_id}",
                                detail={"command_id": command_id})
        return cmd
