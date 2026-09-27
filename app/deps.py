"""FastAPI 依赖装配：单例 storage / services。"""
from __future__ import annotations

import os

from .models import CommandRequest
from .services.audit import AuditService
from .services.device_service import DeviceService
from .services.rule_service import RuleService
from .storage import Storage

DEFAULT_DATA_PATH = os.environ.get("SMART_HOME_DATA", "data/store.json")

_storage: Storage | None = None
device_service: DeviceService | None = None
rule_service: RuleService | None = None
audit_service: AuditService | None = None


def init_services(data_path: str | None = None) -> None:
    """启动时调用：加载存储并装配服务。重复调用会重建（测试用）。"""
    global _storage, device_service, rule_service, audit_service
    _storage = Storage(data_path if data_path is not None else DEFAULT_DATA_PATH)
    _storage.load()
    device_service = DeviceService(_storage)
    audit_service = AuditService(_storage)

    def executor(device_id: str, action: str, params: dict, caller: str) -> dict:
        cmd = device_service.send_command(
            CommandRequest(device_id=device_id, action=action, params=params),
            caller)
        return {"command_id": cmd.command_id, "status": cmd.status.value,
                "reason": cmd.reason}

    rule_service = RuleService(_storage, executor)


def get_storage() -> Storage:
    assert _storage is not None, "init_services() 未调用"
    return _storage
