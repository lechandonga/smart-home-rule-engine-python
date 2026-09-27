"""FastAPI 依赖装配：单例 storage / services。"""
from __future__ import annotations

import os

from .services.audit import AuditService
from .services.device_service import DeviceService
from .services.rule_service import RuleService
from .storage import Storage

_storage: Storage | None = None
device_service: DeviceService | None = None
rule_service: RuleService | None = None
audit_service: AuditService | None = None


def init_services() -> None:
    raise NotImplementedError


def get_storage() -> Storage:
    raise NotImplementedError
