"""FastAPI dependencies: container lookup and authenticated caller."""

from __future__ import annotations

from fastapi import Depends, Header, Request

from app.audit import AuditLog
from app.auth import Caller, CallerRegistry
from app.container import Container
from app.devices import DeviceService
from app.errors import Unauthorized
from app.rules import RuleService


def get_container(request: Request) -> Container:
    return request.app.state.container


def get_caller(
    request: Request,
    x_caller_id: str | None = Header(default=None, alias="X-Caller-Id"),
) -> Caller:
    registry: CallerRegistry = request.app.state.container.registry
    if not x_caller_id:
        raise Unauthorized("缺少 X-Caller-Id 请求头")
    caller = registry.get(x_caller_id)
    if caller is None:
        raise Unauthorized(f"未知调用方: {x_caller_id}")
    return caller


def get_device_service(container: Container = Depends(get_container)) -> DeviceService:
    return container.device_service


def get_rule_service(container: Container = Depends(get_container)) -> RuleService:
    return container.rule_service


def get_audit_log(container: Container = Depends(get_container)) -> AuditLog:
    return container.audit
