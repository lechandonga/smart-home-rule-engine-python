"""Rule lifecycle and the automation engine.

Trigger semantics
-----------------
An event is one accepted state change of a device (report or applied command).
A rule is evaluated for an event only when:

* the rule is enabled,
* the event device appears in the rule's conditions,
* the event actually changed at least one property the rule watches.

All conditions must then match (AND semantics) against the **post-event**
device states. The condition device states are read at evaluation time, so
multi-device rules see one consistent snapshot inside the store lock.

Exactly-once
------------
The event key is ``(device_id, seq)`` for reports and ``("cmd:"+command_id)``
for commands. ``processed_events`` maps each key to the execution id of the
*first* rule that consumed it; a replayed event (duplicate report token,
service restart with the same report retried) returns the stored ids without
creating new executions.

Cycles / references
-------------------
Rules reference devices explicitly. Build/Update time validation rejects:

* references to devices that do not exist (:class:`BrokenReference`),
* condition actions targeting properties unknown to the device type,
* a set of rules whose action/condition graph is cyclic (:class:`RuleCycle`).

Cyclic rules are additionally bounded at runtime by ``MAX_ACTION_DEPTH``.

History / compatibility
-----------------------
Every execution stores the full ``rule_snapshot`` (rule at its then-version),
so updating a rule never rewrites the past: old executions remain reproducible.
"""

from __future__ import annotations

import logging
from typing import Any

from app.audit import AuditLog
from app.auth import Caller, can_access_device, can_manage_rule
from app.errors import (
    BrokenReference,
    Forbidden,
    NotFound,
    RuleCycle,
    ValidationFailed,
)
from app.models import (
    Action,
    ActionResult,
    Condition,
    Device,
    Rule,
    RuleCreate,
    RuleExecution,
    RuleUpdate,
)
from app.store import State, Store
from app.util import describe_state, new_id, now_iso, validate_state

log = logging.getLogger("smart_home.rules")

MAX_ACTION_DEPTH = 8

_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
}


def event_key(device_id: str, seq: int, caused_by_command: str | None) -> str:
    if caused_by_command is not None:
        return f"cmd:{caused_by_command}"
    return f"dev:{device_id}:{seq}"


def _check_property_known(device: Device, prop: str, *, where: str) -> None:
    from app.models import DEVICE_STATE_SPEC

    spec = DEVICE_STATE_SPEC[device.type]
    if prop not in spec:
        raise ValidationFailed(
            f"{where}: 设备 {device.device_id}（{device.type.value}）没有属性 {prop!r}",
            details={"device_id": device.device_id, "allowed": list(spec), "property": prop},
        )


class RuleEngine:
    def __init__(self, store: Store, device_service: "DeviceServiceLike") -> None:  # noqa: F821
        self.store = store
        self.devices = device_service

    # --------------------------------------------------------- graph checks

    @staticmethod
    def find_cycle(st: State, candidate: Rule | None = None) -> list[str] | None:
        """Return a cycle path (rule ids) if one exists, else None.

        Graph: an edge rule X -> rule Y exists if X writes a device (action)
        that Y watches (condition). A rule reading & writing the same watched
        device is a self-loop and also a cycle.
        """
        rules = dict(st.rules)
        if candidate is not None:
            rules[candidate.rule_id] = candidate

        edges: dict[str, set[str]] = {rid: set() for rid in rules}
        for rid, rule in rules.items():
            written = {a.device_id for a in rule.actions}
            watched = {c.device_id for c in rule.conditions}
            for other_id, other in rules.items():
                other_watches = {c.device_id for c in other.conditions}
                if written & other_watches:
                    edges[rid].add(other_id)

        color: dict[str, int] = {rid: 0 for rid in rules}  # 0 white 1 gray 2 black
        stack: list[str] = []

        def dfs(node: str) -> list[str] | None:
            color[node] = 1
            stack.append(node)
            for nxt in edges[node]:
                if color[nxt] == 1:
                    start = stack.index(nxt)
                    return stack[start:] + [nxt]
                if color[nxt] == 0:
                    found = dfs(nxt)
                    if found:
                        return found
            stack.pop()
            color[node] = 2
            return None

        for rid in rules:
            if color[rid] == 0:
                cyc = dfs(rid)
                if cyc:
                    return cyc
        return None

    # --------------------------------------------------------- per-rule eval

    @staticmethod
    def _condition_matches(cond: Condition, st: State, trace: list[str]) -> bool:
        dev = st.devices.get(cond.device_id)
        if dev is None:
            trace.append(f"条件设备 {cond.device_id} 已不存在，按不匹配处理")
            return False
        if cond.property not in dev.state:
            trace.append(
                f"条件 {cond.device_id}.{cond.property} {cond.op} {cond.value!r}："
                "设备尚无该属性，不匹配"
            )
            return False
        actual = dev.state[cond.property]
        try:
            matched = _OPS[cond.op](actual, cond.value)
        except TypeError:
            matched = False
            trace.append(
                f"条件 {cond.device_id}.{cond.property} {cond.op} {cond.value!r}："
                f"实际值 {actual!r} 类型不可比较 -> 不匹配"
            )
        else:
            trace.append(
                f"条件 {cond.device_id}.{cond.property} {cond.op} {cond.value!r}："
                f"实际值 {actual!r} -> {'匹配' if matched else '不匹配'}"
            )
        return matched

    def evaluate_event(
        self,
        st: State,
        *,
        device_id: str,
        seq: int,
        state: dict[str, Any],
        prev_state: dict[str, Any],
        caused_by_command: str | None = None,
        _depth: int = 0,
    ) -> list[str]:
        """Evaluate one state-change event. Must be called inside a mutation."""
        if _depth > MAX_ACTION_DEPTH:
            log.error("规则动作级联超过最大深度 %d，停止继续触发", MAX_ACTION_DEPTH)
            return []

        key = event_key(device_id, seq, caused_by_command)

        # Exactly-once: first ever sighting of this key is processed below;
        # a repeat returns the already-recorded execution ids.
        prior = st.processed_events.get(key)
        if prior is not None:
            if prior == "":
                # Re-entrant evaluation of the same event from a cascading
                # action (should be prevented by the cycle check at write time,
                # but guarded here for safety): never recurse.
                log.warning("event re-entrant key=%s -> skip", key)
                return []
            log.info("event duplicate key=%s -> executions=%s (no re-trigger)", key, prior)
            return list(prior)

        changed = {k for k in set(state) | set(prev_state)
                   if state.get(k) != prev_state.get(k)}
        log.info(
            "event evaluate key=%s device=%s changed=%s depth=%d",
            key, device_id, sorted(changed), _depth,
        )

        # Reserve the key so recursive events from actions cannot re-enter it.
        st.processed_events[key] = ""
        produced: list[str] = []

        # Deterministic ordering: rule id.
        for rule in sorted(st.rules.values(), key=lambda r: r.rule_id):
            if not rule.enabled:
                continue
            watched_devs = {c.device_id for c in rule.conditions}
            watched_props = {c.property for c in rule.conditions if c.device_id == device_id}
            if device_id not in watched_devs:
                continue
            if not (watched_props & changed):
                continue

            trace = [
                f"事件设备={device_id} 变更字段={sorted(changed)} "
                f"规则关注字段={sorted(watched_props)}",
            ]
            matches = [self._condition_matches(c, st, trace) for c in rule.conditions]
            matched = all(matches)
            action_results: list[ActionResult] = []
            status = "failed_no_match"

            if matched:
                status = "executed"
                trace.append(f"全部 {len(matches)} 个条件匹配，开始执行 {len(rule.actions)} 个动作")
                for action in rule.actions:
                    target = st.devices.get(action.device_id)
                    if target is None:
                        ar = ActionResult(
                            device_id=action.device_id, params=dict(action.params),
                            status="rejected", reason="target_missing",
                        )
                        trace.append(f"动作目标 {action.device_id} 不存在 -> rejected(target_missing)")
                        action_results.append(ar)
                        continue
                    if target.status.value == "offline":
                        ar = ActionResult(
                            device_id=action.device_id, params=dict(action.params),
                            status="rejected", reason="device_offline",
                        )
                        trace.append(f"动作目标 {action.device_id} 离线 -> rejected(device_offline)")
                        action_results.append(ar)
                        continue
                    from app.models import CommandRequest
                    from app.auth import Caller

                    system = Caller(caller_id=f"rule:{rule.rule_id}", name="Rule engine",
                                    role="user", allowed_devices=[a.device_id for a in rule.actions])
                    cmd = CommandRequest(
                        command_id=new_id(f"rcmd-{rule.rule_id}"),
                        device_id=action.device_id,
                        params=dict(action.params),
                    )
                    res = self.devices.send_command(
                        cmd, system, source_rule_id=rule.rule_id, st=st,
                    )
                    ar = ActionResult(
                        device_id=action.device_id,
                        params=dict(action.params),
                        status=res.command.status,
                        reason=res.command.reason,
                    )
                    trace.append(
                        f"动作 -> {action.device_id} params={action.params} "
                        f"=> {ar.status}({ar.reason})"
                    )
                    action_results.append(ar)
            else:
                trace.append("条件未全部满足，不执行动作")

            execution = RuleExecution(
                execution_id=new_id("exec"),
                event_key=key,
                rule_id=rule.rule_id,
                rule_version=rule.version,
                rule_snapshot=rule.model_copy(deep=True),
                triggered_at=now_iso(),
                event_device_id=device_id,
                event_seq=seq,
                event_state=dict(state),
                matched=matched,
                status=status,
                action_results=action_results,
                trace=trace,
            )
            st.executions[execution.execution_id] = execution
            produced.append(execution.execution_id)
            log.info(
                "rule evaluated rule=%s v%d matched=%s status=%s execution=%s",
                rule.rule_id, rule.version, matched, status, execution.execution_id,
            )

        st.processed_events[key] = produced
        return produced


class RuleService:
    def __init__(self, store: Store, registry: Any, audit: AuditLog,
                 engine: RuleEngine) -> None:
        self.store = store
        self.registry = registry
        self.audit = audit
        self.engine = engine

    # ------------------------------------------------------------ helpers

    def _audit(self, caller: Caller, action: str, resource_id: str,
               allowed: bool, reason: str | None = None,
               details: dict[str, Any] | None = None) -> None:
        from app.models import AuditEntry

        self.audit.append(AuditEntry(
            audit_id=new_id("aud"), ts=now_iso(), caller_id=caller.caller_id,
            action=action, resource_type="rule", resource_id=resource_id,
            result="allowed" if allowed else "denied", reason=reason,
            details=details or {},
        ))

    def _validate_rule_shape(self, st: State, rule: Rule, *, caller: Caller) -> None:
        if not rule.conditions:
            raise ValidationFailed("规则至少需要一个触发条件")
        if not rule.actions:
            raise ValidationFailed("规则至少需要一个动作")

        cond_devices: set[str] = set()
        for c in rule.conditions:
            dev = st.devices.get(c.device_id)
            if dev is None:
                raise BrokenReference(
                    f"条件引用了不存在的设备: {c.device_id}",
                    details={"missing_device": c.device_id},
                )
            _check_property_known(dev, c.property, where="规则条件")
            cond_devices.add(c.device_id)

        for a in rule.actions:
            dev = st.devices.get(a.device_id)
            if dev is None:
                raise BrokenReference(
                    f"动作引用了不存在的设备: {a.device_id}",
                    details={"missing_device": a.device_id},
                )
            validate_state(dev.type, a.params)
            if not can_access_device(caller, dev):
                raise Forbidden(
                    f"调用方 {caller.caller_id} 无权让规则操作设备 {a.device_id}"
                )
        # Caller must also be allowed to read every condition device.
        for did in cond_devices:
            if not can_access_device(caller, st.devices[did]):
                raise Forbidden(
                    f"调用方 {caller.caller_id} 无权监听设备 {did} 的状态"
                )

        cycle = self.engine.find_cycle(st, candidate=rule)
        if cycle:
            raise RuleCycle(
                "规则之间存在循环依赖：" + " -> ".join(cycle),
                details={"cycle": cycle},
            )

    # ------------------------------------------------------------- writes

    def create_rule(self, req: RuleCreate, caller: Caller) -> Rule:
        def _do(st: State) -> Rule:
            if req.rule_id in st.rules:
                from app.errors import Conflict
                raise Conflict(f"规则已存在: {req.rule_id}")
            rule = Rule(
                rule_id=req.rule_id,
                name=req.name,
                enabled=req.enabled,
                conditions=list(req.conditions),
                actions=list(req.actions),
                version=1,
                created_by=caller.caller_id,
                created_at=now_iso(),
                updated_at=now_iso(),
            )
            self._validate_rule_shape(st, rule, caller=caller)
            st.rules[rule.rule_id] = rule
            log.info(
                "create_rule caller=%s rule=%s conditions=%d actions=%d",
                caller.caller_id, rule.rule_id, len(rule.conditions), len(rule.actions),
            )
            return rule.model_copy(deep=True)

        rule = self.store.mutate(_do)
        self._audit(caller, "create_rule", rule.rule_id, True,
                    details={"version": rule.version})
        return rule

    def update_rule(self, rule_id: str, req: RuleUpdate, caller: Caller) -> Rule:
        snap = self.store.snapshot()
        existing = snap.rules.get(rule_id)
        if existing is None:
            raise NotFound(f"规则不存在: {rule_id}")
        if not can_manage_rule(caller, existing):
            self._audit(caller, "update_rule", rule_id, False,
                        reason="非规则创建者且非管理员")
            raise Forbidden(f"调用方 {caller.caller_id} 无权修改规则 {rule_id}")

        def _do(st: State) -> Rule:
            rule = st.rules[rule_id]
            updated = rule.model_copy(deep=True)
            if req.name is not None:
                updated.name = req.name
            if req.enabled is not None:
                updated.enabled = req.enabled
            if req.conditions is not None:
                updated.conditions = list(req.conditions)
            if req.actions is not None:
                updated.actions = list(req.actions)
            updated.version = rule.version + 1
            updated.updated_at = now_iso()
            self._validate_rule_shape(st, updated, caller=caller)
            st.rules[rule_id] = updated
            log.info(
                "update_rule caller=%s rule=%s %d->%d enabled=%s",
                caller.caller_id, rule_id, rule.version, updated.version, updated.enabled,
            )
            return updated.model_copy(deep=True)

        rule = self.store.mutate(_do)
        self._audit(caller, "update_rule", rule_id, True,
                    details={"version": rule.version, "enabled": rule.enabled})
        return rule

    # -------------------------------------------------------------- reads

    def get_rule(self, rule_id: str, caller: Caller) -> Rule:
        snap = self.store.snapshot()
        rule = snap.rules.get(rule_id)
        if rule is None:
            raise NotFound(f"规则不存在: {rule_id}")
        if not can_manage_rule(caller, rule):
            self._audit(caller, "get_rule", rule_id, False,
                        reason="非规则创建者且非管理员")
            raise Forbidden(f"调用方 {caller.caller_id} 无权查看规则 {rule_id}")
        self._audit(caller, "get_rule", rule_id, True)
        return rule

    def list_rules(self, caller: Caller) -> list[Rule]:
        snap = self.store.snapshot()
        rules = [r for r in snap.rules.values() if can_manage_rule(caller, r)]
        self._audit(caller, "list_rules", "*", True,
                    details={"matched": len(rules)})
        return rules

    def list_executions(self, rule_id: str, caller: Caller) -> list[RuleExecution]:
        rule = self.get_rule(rule_id, caller)
        snap = self.store.snapshot()
        executions = [
            e for e in snap.executions.values()
            if e.rule_id == rule.rule_id
        ]
        executions.sort(key=lambda e: e.triggered_at)
        return executions
