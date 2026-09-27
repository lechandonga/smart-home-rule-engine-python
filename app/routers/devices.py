"""设备与命令路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import deps
from ..auth import Caller
from ..errors import ForbiddenError
from ..models import Command, CommandRequest, CommandStatus, Device, StateReport
from . import get_caller, require_device_scope

router = APIRouter(tags=["devices"])


class StatusRequest(BaseModel):
    online: bool


@router.post("/", response_model=Device)
def register_device(device: Device,
                    caller: Caller = Depends(get_caller)) -> Device:
    if not caller.admin:
        deps.audit_service.record(caller.name, "device.register",
                                  device.device_id, "deny", "仅管理员可注册设备")
        raise ForbiddenError("仅管理员可注册设备")
    return deps.device_service.register_device(device, caller.name)


@router.get("/", response_model=list[Device])
def list_devices(caller: Caller = Depends(get_caller)) -> list[Device]:
    return [d for d in deps.device_service.list_devices()
            if caller.can_device(d.device_id)]


@router.get("/{device_id}", response_model=Device)
def get_device(device_id: str,
               caller: Caller = Depends(get_caller)) -> Device:
    require_device_scope(caller, device_id, "device.get")
    return deps.device_service.get_device(device_id)


@router.post("/{device_id}/status", response_model=Device)
def set_status(device_id: str, req: StatusRequest,
               caller: Caller = Depends(get_caller)) -> Device:
    require_device_scope(caller, device_id, "device.status")
    return deps.device_service.set_status(device_id, req.online, caller.name)


@router.post("/{device_id}/state", response_model=Device)
def report_state(device_id: str, report: StateReport,
                 caller: Caller = Depends(get_caller)) -> Device:
    require_device_scope(caller, device_id, "device.report_state")
    report.device_id = device_id
    device = deps.device_service.report_state(report, caller.name)
    # 状态变化触发规则引擎（规则动作本身不再级联触发，见 README）
    deps.rule_service.on_state_changed(device, dict(report.state))
    return device


@router.post("/commands", response_model=Command)
def send_command(req: CommandRequest,
                 caller: Caller = Depends(get_caller)) -> Command:
    require_device_scope(caller, req.device_id, "device.command")
    cmd = deps.device_service.send_command(req, caller.name)
    deps.audit_service.record(caller.name, "device.command", req.device_id,
                              "allow",
                              f"{cmd.command_id} {cmd.action} -> {cmd.status.value}")
    if cmd.status == CommandStatus.EXECUTED and cmd.action == "set":
        device = deps.device_service.get_device(req.device_id)
        deps.rule_service.on_state_changed(device, dict(req.params))
    return cmd


@router.get("/commands/{command_id}", response_model=Command)
def get_command(command_id: str,
                caller: Caller = Depends(get_caller)) -> Command:
    cmd = deps.device_service.get_command(command_id)
    require_device_scope(caller, cmd.device_id, "device.command.get")
    return cmd
