"""自动化规则：CRUD、静态校验、事件触发执行。

语义约定：
- 创建/更新时校验：trigger 与 actions 引用的设备必须存在；
  规则之间按"动作写某设备属性 -> 触发另一规则"建图，出现环即拒绝。
- 事件去重键 event_key = "{device_id}:{state_version}"，
  已处理事件键持久化，重启或重复投递都不会重复执行。
- 每次执行记录规则当时的 revision 与触发快照，更新规则不影响历史回放。
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from ..errors import NotFoundError, ValidationFailedError
from ..models import Device, Rule, RuleExecution, utcnow
from ..storage import Storage

logger = logging.getLogger("smart_home.rule")

# 动作执行器：(device_id, action, params, caller) -> 结果 dict
ActionExecutor = Callable[[str, str, dict, str], dict]

MAX_TRIGGER_DEPTH = 8


class RuleService:
    def __init__(self, storage: Storage, executor: ActionExecutor | None = None):
        self.storage = storage
        self.executor = executor

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def _validate_rule(self, rule: Rule) -> None:
        devices = self.storage.data.devices
        missing = []
        if rule.trigger.device_id not in devices:
            missing.append(rule.trigger.device_id)
        for act in rule.actions:
            if act.device_id not in devices:
                missing.append(act.device_id)
        if missing:
            raise ValidationFailedError(
                f"规则 {rule.rule_id} 引用了不存在的设备: {sorted(set(missing))}",
                detail={"missing_devices": sorted(set(missing))},
            )
        self._check_cycle(rule)

    def _check_cycle(self, candidate: Rule) -> None:
        """按 动作(设备,属性) -> 命中其他规则 trigger 建图，DFS 找环。"""
        rules = {r.rule_id: r for r in self.storage.data.rules.values()}
        rules[candidate.rule_id] = candidate  # 用候选版本覆盖旧版本

        def edges(r: Rule) -> list[str]:
            out = []
            for act in r.actions:
                for other in rules.values():
                    if (other.rule_id != r.rule_id
                            and other.trigger.device_id == act.device_id
                            and act.action == "set"
                            and other.trigger.property in act.params):
                        out.append(other.rule_id)
            # 自环：动作直接写自己的 trigger 属性
            if any(act.device_id == r.trigger.device_id and act.action == "set"
                   and r.trigger.property in act.params for act in r.actions):
                out.append(r.rule_id)
            return out

        def visit(node: str, stack: list[str]) -> None:
            if node in stack:
                cycle = stack[stack.index(node):] + [node]
                raise ValidationFailedError(
                    f"规则存在循环依赖: {' -> '.join(cycle)}",
                    detail={"cycle": cycle},
                )
            for nxt in edges(rules[node]):
                visit(nxt, stack + [node])

        for rid in rules:
            visit(rid, [])

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def create_rule(self, rule: Rule, caller: str) -> Rule:
        with self.storage.lock:
            if rule.rule_id in self.storage.data.rules:
                raise ValidationFailedError(
                    f"规则已存在: {rule.rule_id}",
                    detail={"rule_id": rule.rule_id})
            self._validate_rule(rule)
            self.storage.data.rules[rule.rule_id] = rule
            self.storage.save()
            logger.info("rule %s created by %s: %s", rule.rule_id, caller,
                        rule.model_dump())
            return rule

    def get_rule(self, rule_id: str) -> Rule:
        rule = self.storage.data.rules.get(rule_id)
        if rule is None:
            raise NotFoundError(f"规则不存在: {rule_id}",
                                detail={"rule_id": rule_id})
        return rule

    def list_rules(self) -> list[Rule]:
        return list(self.storage.data.rules.values())

    def update_rule(self, rule_id: str, rule: Rule, caller: str) -> Rule:
        with self.storage.lock:
            old = self.get_rule(rule_id)
            rule.rule_id = rule_id
            rule.revision = old.revision + 1  # 历史执行记录仍指向旧 revision
            rule.created_at = old.created_at
            rule.updated_at = utcnow()
            self._validate_rule(rule)
            self.storage.data.rules[rule_id] = rule
            self.storage.save()
            logger.info("rule %s updated by %s: revision %d -> %d",
                        rule_id, caller, old.revision, rule.revision)
            return rule

    def set_enabled(self, rule_id: str, enabled: bool, caller: str) -> Rule:
        with self.storage.lock:
            rule = self.get_rule(rule_id)
            rule.enabled = enabled
            rule.updated_at = utcnow()
            self.storage.save()
            logger.info("rule %s enabled=%s by %s", rule_id, enabled, caller)
            return rule

    def list_executions(self, rule_id: str | None = None) -> list[RuleExecution]:
        execs = self.storage.data.executions.values()
        if rule_id is not None:
            execs = [e for e in execs if e.rule_id == rule_id]
        return sorted(execs, key=lambda e: e.executed_at)

    # ------------------------------------------------------------------
    # 触发引擎
    # ------------------------------------------------------------------
    @staticmethod
    def _matches(rule: Rule, device: Device, changed: dict) -> bool:
        t = rule.trigger
        if t.device_id != device.device_id or t.property not in changed:
            return False
        current = device.state.get(t.property)
        if t.op == "eq":
            return current == t.value
        if t.op == "ne":
            return current != t.value
        try:
            if t.op == "gt":
                return current > t.value
            if t.op == "lt":
                return current < t.value
        except TypeError:
            return False
        return False

    def on_state_changed(self, device: Device, changed: dict,
                         _depth: int = 0) -> list[RuleExecution]:
        """设备状态变化后评估规则。同一事件键只执行一次。"""
        if _depth >= MAX_TRIGGER_DEPTH:
            logger.warning("trigger depth limit reached at %s", device.device_id)
            return []
        event_key = f"{device.device_id}:{device.state_version}"
        with self.storage.lock:
            if event_key in self.storage.data.processed_events:
                logger.info("event %s already processed, skip", event_key)
                return []
            self.storage.data.processed_events.append(event_key)
            fired: list[Rule] = [
                r for r in self.storage.data.rules.values()
                if r.enabled and self._matches(r, device, changed)
            ]
            executions: list[RuleExecution] = []
            for rule in fired:
                results = []
                for act in rule.actions:
                    caller = f"rule:{rule.rule_id}"
                    if self.executor is None:
                        results.append({"action": act.action, "skipped": True})
                        continue
                    try:
                        results.append(self.executor(act.device_id, act.action,
                                                     act.params, caller))
                    except Exception as exc:  # 动作失败也留痕
                        results.append({"action": act.action,
                                        "error": str(exc)})
                exe = RuleExecution(
                    execution_id=uuid.uuid4().hex,
                    rule_id=rule.rule_id,
                    rule_revision=rule.revision,
                    event_key=event_key,
                    trigger_snapshot={
                        "device_id": device.device_id,
                        "state_version": device.state_version,
                        "changed": dict(changed),
                        "state": dict(device.state),
                    },
                    results=results,
                )
                self.storage.data.executions[exe.execution_id] = exe
                executions.append(exe)
                logger.info("rule %s fired by event %s (rev %d)",
                            rule.rule_id, event_key, rule.revision)
            self.storage.save()
        return executions
