"""自动化规则路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import deps
from ..auth import Caller
from ..errors import ForbiddenError
from ..models import Rule, RuleExecution
from . import get_caller, require_rule_scope

router = APIRouter(tags=["rules"])


class EnabledRequest(BaseModel):
    enabled: bool


@router.post("/", response_model=Rule)
def create_rule(rule: Rule, caller: Caller = Depends(get_caller)) -> Rule:
    require_rule_scope(caller, rule.rule_id, "rule.create")
    created = deps.rule_service.create_rule(rule, caller.name)
    deps.audit_service.record(caller.name, "rule.create", rule.rule_id, "allow",
                              f"revision={created.revision}")
    return created


@router.get("/", response_model=list[Rule])
def list_rules(caller: Caller = Depends(get_caller)) -> list[Rule]:
    return [r for r in deps.rule_service.list_rules()
            if caller.can_rule(r.rule_id)]


@router.get("/executions", response_model=list[RuleExecution])
def list_executions(caller: Caller = Depends(get_caller)) -> list[RuleExecution]:
    return [e for e in deps.rule_service.list_executions()
            if caller.can_rule(e.rule_id)]


@router.get("/{rule_id}", response_model=Rule)
def get_rule(rule_id: str, caller: Caller = Depends(get_caller)) -> Rule:
    require_rule_scope(caller, rule_id, "rule.get")
    return deps.rule_service.get_rule(rule_id)


@router.put("/{rule_id}", response_model=Rule)
def update_rule(rule_id: str, rule: Rule,
                caller: Caller = Depends(get_caller)) -> Rule:
    require_rule_scope(caller, rule_id, "rule.update")
    updated = deps.rule_service.update_rule(rule_id, rule, caller.name)
    deps.audit_service.record(caller.name, "rule.update", rule_id, "allow",
                              f"revision={updated.revision}")
    return updated


@router.post("/{rule_id}/enabled", response_model=Rule)
def set_enabled(rule_id: str, req: EnabledRequest,
                caller: Caller = Depends(get_caller)) -> Rule:
    require_rule_scope(caller, rule_id, "rule.enable")
    updated = deps.rule_service.set_enabled(rule_id, req.enabled, caller.name)
    deps.audit_service.record(caller.name, "rule.enable", rule_id, "allow",
                              f"enabled={req.enabled}")
    return updated
