"""路由公共依赖：认证与授权。"""
from __future__ import annotations

from fastapi import Header

from .. import deps
from ..auth import Caller, authenticate
from ..errors import ForbiddenError


def get_caller(x_api_key: str | None = Header(default=None)) -> Caller:
    return authenticate(x_api_key)


def require_device_scope(caller: Caller, device_id: str, operation: str) -> None:
    """越权访问：拒绝并留审计记录。"""
    if not caller.can_device(device_id):
        deps.audit_service.record(caller.name, operation, device_id, "deny",
                                  "越权访问设备")
        raise ForbiddenError(f"无权操作设备 {device_id}",
                             detail={"device_id": device_id})


def require_rule_scope(caller: Caller, rule_id: str, operation: str) -> None:
    if not caller.can_rule(rule_id):
        deps.audit_service.record(caller.name, operation, rule_id, "deny",
                                  "越权访问规则")
        raise ForbiddenError(f"无权操作规则 {rule_id}",
                             detail={"rule_id": rule_id})
