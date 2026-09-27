"""自动化规则 CRUD、校验与触发引擎。空实现骨架。"""
from __future__ import annotations

from ..models import Device, Rule, RuleExecution
from ..storage import Storage


class RuleService:
    def __init__(self, storage: Storage):
        self.storage = storage

    def create_rule(self, rule: Rule, caller: str) -> Rule:
        raise NotImplementedError

    def get_rule(self, rule_id: str) -> Rule:
        raise NotImplementedError

    def list_rules(self) -> list[Rule]:
        raise NotImplementedError

    def update_rule(self, rule_id: str, rule: Rule, caller: str) -> Rule:
        raise NotImplementedError

    def set_enabled(self, rule_id: str, enabled: bool, caller: str) -> Rule:
        raise NotImplementedError

    def list_executions(self, rule_id: str | None = None) -> list[RuleExecution]:
        raise NotImplementedError

    def on_state_changed(self, device: Device, changed: dict) -> list[RuleExecution]:
        """设备状态变化后调用，评估并执行命中的规则。"""
        raise NotImplementedError
