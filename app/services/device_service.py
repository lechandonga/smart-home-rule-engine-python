"""设备注册、状态上报、命令下发。空实现骨架。"""
from __future__ import annotations

from ..models import Command, CommandRequest, Device, StateReport
from ..storage import Storage


class DeviceService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def register_device(self, device: Device, caller: str) -> Device:
        raise NotImplementedError

    def get_device(self, device_id: str) -> Device:
        raise NotImplementedError

    def list_devices(self) -> list[Device]:
        raise NotImplementedError

    def set_status(self, device_id: str, online: bool, caller: str) -> Device:
        raise NotImplementedError

    def report_state(self, report: StateReport, caller: str) -> Device:
        raise NotImplementedError

    def send_command(self, req: CommandRequest, caller: str) -> Command:
        raise NotImplementedError

    def get_command(self, command_id: str) -> Command:
        raise NotImplementedError
