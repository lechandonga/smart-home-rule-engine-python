"""Audit query HTTP API."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.audit import AuditLog
from app.auth import Caller
from app.deps import get_audit_log, get_caller

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("")
def list_audit(
    limit: int = 100,
    log: AuditLog = Depends(get_audit_log),
    caller: Caller = Depends(get_caller),
):
    return log.list(limit)
