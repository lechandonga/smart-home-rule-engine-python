"""审计日志查询路由（仅管理员）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import deps
from ..auth import Caller
from ..errors import ForbiddenError
from ..models import AuditEntry
from . import get_caller

router = APIRouter(tags=["audit"])


@router.get("/", response_model=list[AuditEntry])
def list_audit(caller: Caller = Depends(get_caller)) -> list[AuditEntry]:
    if not caller.admin:
        deps.audit_service.record(caller.name, "audit.list", "*", "deny",
                                  "仅管理员可查看审计日志")
        raise ForbiddenError("仅管理员可查看审计日志")
    return deps.audit_service.list_entries()
