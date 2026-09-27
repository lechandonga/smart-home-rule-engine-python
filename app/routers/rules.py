"""Rule HTTP API (thin transport layer; logic lives in RuleService)."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import Caller
from app.deps import get_caller, get_rule_service
from app.models import RuleCreate, RuleUpdate
from app.rules import RuleService

router = APIRouter(prefix="/rules", tags=["rules"])


@router.post("")
def create_rule(
    body: RuleCreate,
    svc: RuleService = Depends(get_rule_service),
    caller: Caller = Depends(get_caller),
):
    return svc.create_rule(body, caller)


@router.get("")
def list_rules(
    svc: RuleService = Depends(get_rule_service),
    caller: Caller = Depends(get_caller),
):
    return svc.list_rules(caller)


@router.get("/{rule_id}")
def get_rule(
    rule_id: str,
    svc: RuleService = Depends(get_rule_service),
    caller: Caller = Depends(get_caller),
):
    return svc.get_rule(rule_id, caller)


@router.put("/{rule_id}")
def update_rule(
    rule_id: str,
    body: RuleUpdate,
    svc: RuleService = Depends(get_rule_service),
    caller: Caller = Depends(get_caller),
):
    return svc.update_rule(rule_id, body, caller)


@router.get("/{rule_id}/executions")
def list_executions(
    rule_id: str,
    svc: RuleService = Depends(get_rule_service),
    caller: Caller = Depends(get_caller),
):
    return svc.list_executions(rule_id, caller)
