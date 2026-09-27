"""Rule engine: exactly-once events, restart, cycles, snapshots, tracing."""

from __future__ import annotations

from app.errors import RuleCycle, BrokenReference
from app.models import (
    DeviceStatus,
    RegisterDeviceRequest,
    RuleCreate,
    Condition,
    Action,
    StateReport,
    RuleUpdate,
)
from tests.conftest import log_verdict, run_concurrently


def _devices(container, admin):
    svc = container.device_service
    svc.register_device(RegisterDeviceRequest(
        device_id="sensor-hall", name="hall", type="sensor", initial_state={}), admin)
    svc.register_device(RegisterDeviceRequest(
        device_id="light-living", name="light", type="light",
        initial_state={"power": False}), admin)
    svc.register_device(RegisterDeviceRequest(
        device_id="switch-fan", name="fan", type="switch",
        initial_state={"power": False}), admin)


def _motion_rule(callbacks=True):
    return RuleCreate(
        rule_id="r-motion-light", name="motion light",
        conditions=[Condition(device_id="sensor-hall", property="motion",
                              op="eq", value=True)],
        actions=[Action(device_id="light-living", params={"power": True})],
    )


def test_event_triggers_rule_exactly_once(container, admin, caplog):
    _devices(container, admin)
    container.rule_service.create_rule(_motion_rule(), admin)

    r = container.device_service.report_state(
        StateReport(device_id="sensor-hall", state={"motion": True},
                    seq=1, token="t1"), admin)
    assert len(r.rule_execution_ids) == 1
    light = container.store.snapshot().devices["light-living"]
    assert light.state["power"] is True

    # Replay the same event (duplicate report token) -> same execution, no rerun.
    replay = container.device_service.report_state(
        StateReport(device_id="sensor-hall", state={"motion": True},
                    seq=1, token="t1"), admin)
    assert replay.reason == "duplicate"
    assert replay.rule_execution_ids == r.rule_execution_ids
    execs = container.rule_service.list_executions("r-motion-light", admin)
    assert len(execs) == 1
    log_verdict(caplog, "event_once",
                {"event": "sensor-hall seq=1 motion=True, token t1 投递两次"},
                {"execution_ids": r.rule_execution_ids,
                 "light_power": light.state["power"], "execution_count": len(execs)},
                "同事件只产生一条 execution；重放回显原 execution，动作不重复执行")


def test_duplicate_event_after_restart(container, admin, data_dir, caplog):
    _devices(container, admin)
    container.rule_service.create_rule(_motion_rule(), admin)
    first = container.device_service.report_state(
        StateReport(device_id="sensor-hall", state={"motion": True},
                    seq=1, token="t1"), admin)
    before = len(container.store.snapshot().executions)

    # Simulate service restart: rebuild the whole container from disk.
    from app.container import Container
    restarted = Container(data_dir)
    replay = restarted.device_service.report_state(
        StateReport(device_id="sensor-hall", state={"motion": True},
                    seq=1, token="t1"), admin)
    after = len(restarted.store.snapshot().executions)
    assert replay.reason == "duplicate"
    assert replay.rule_execution_ids == first.rule_execution_ids
    assert before == after == 1
    log_verdict(caplog, "event_once_across_restart",
                {"replayed_after_restart": "token t1 / seq 1"},
                {"executions_before": before, "executions_after": after},
                "processed_events 持久化，重启后重复事件仍不产生新执行")


def test_concurrent_same_event_fires_once(container, admin, caplog):
    _devices(container, admin)
    container.rule_service.create_rule(_motion_rule(), admin)

    def task(_i):
        return container.device_service.report_state(
            StateReport(device_id="sensor-hall", state={"motion": True},
                        seq=1, token="t1"), admin)

    results = run_concurrently(task, 12)
    reasons = sorted((r.reason or "accepted") for r in results)
    execs = container.rule_service.list_executions("r-motion-light", admin)
    assert reasons.count("accepted") == 1 and reasons.count("duplicate") == 11
    assert len(execs) == 1
    log_verdict(caplog, "concurrent_event",
                {"threads": 12, "same token/seq": "t1/1"},
                {"reasons": reasons, "execution_count": len(execs)},
                "并发重复事件经单锁串行化：仅一次 accepted、一次执行")


def test_rule_cycle_rejected(container, admin, caplog):
    _devices(container, admin)
    # r1 writes light; r2 watches light and writes sensor-hall, which r1 watches.
    r1 = RuleCreate(rule_id="r1", name="sensor->light",
                    conditions=[Condition(device_id="sensor-hall", property="motion",
                                          op="eq", value=True)],
                    actions=[Action(device_id="light-living", params={"power": True})])
    r2 = RuleCreate(rule_id="r2", name="light->sensor",
                    conditions=[Condition(device_id="light-living", property="power",
                                          op="eq", value=True)],
                    actions=[Action(device_id="sensor-hall", params={"motion": False})])
    container.rule_service.create_rule(r1, admin)
    raised = None
    try:
        container.rule_service.create_rule(r2, admin)
    except RuleCycle as e:
        raised = e
    assert raised is not None
    assert "r1" in raised.details["cycle"] and "r2" in raised.details["cycle"]
    assert "r2" not in container.store.snapshot().rules
    log_verdict(caplog, "rule_cycle",
                {"rules": ["r1: sensor->light", "r2: light->sensor"]},
                {"cycle": raised.details["cycle"],
                 "persisted_rules": list(container.store.snapshot().rules)},
                "动作/条件图存在环 -> rule_cycle 拒绝，r2 不落库")


def test_self_loop_rejected(container, admin, caplog):
    _devices(container, admin)
    rule = RuleCreate(
        rule_id="r-self", name="self",
        conditions=[Condition(device_id="light-living", property="power",
                              op="eq", value=True)],
        actions=[Action(device_id="light-living", params={"power": False})])
    try:
        container.rule_service.create_rule(rule, admin)
        assert False
    except RuleCycle as e:
        cycle = e.details["cycle"]
    assert cycle[0] == cycle[-1] == "r-self"
    log_verdict(caplog, "self_loop", {"rule": "watches & writes light-living"},
                {"cycle": cycle}, "自环也判为循环依赖")


def test_broken_reference_rejected(container, admin, caplog):
    _devices(container, admin)
    bad_condition = RuleCreate(
        rule_id="r-bad-cond", name="x",
        conditions=[Condition(device_id="ghost", property="motion",
                              op="eq", value=True)],
        actions=[Action(device_id="light-living", params={"power": True})])
    bad_action = RuleCreate(
        rule_id="r-bad-act", name="y",
        conditions=[Condition(device_id="sensor-hall", property="motion",
                              op="eq", value=True)],
        actions=[Action(device_id="ghost", params={})])
    for bad, missing in ((bad_condition, "ghost"), (bad_action, "ghost")):
        try:
            container.rule_service.create_rule(bad, admin)
            assert False
        except BrokenReference as e:
            assert e.details["missing_device"] == missing
    log_verdict(caplog, "broken_reference",
                {"rules": ["r-bad-cond", "r-bad-act"]},
                {"persisted_rules": list(container.store.snapshot().rules)},
                "条件/动作引用不存在设备 -> broken_reference，规则不落库")


def test_execution_keeps_rule_snapshot_after_update(container, admin, caplog):
    _devices(container, admin)
    created = container.rule_service.create_rule(_motion_rule(), admin)
    container.device_service.report_state(
        StateReport(device_id="sensor-hall", state={"motion": True},
                    seq=1, token="t1"), admin)
    # Change the rule: new condition + disabled, version bumps.
    updated = container.rule_service.update_rule(
        "r-motion-light",
        RuleUpdate(enabled=False,
                   conditions=[Condition(device_id="switch-fan", property="power",
                                         op="eq", value=True)]),
        admin)
    execs = container.rule_service.list_executions("r-motion-light", admin)
    old = execs[0]
    assert old.rule_version == 1
    assert old.rule_snapshot.conditions[0].device_id == "sensor-hall"
    assert updated.version == 2 and updated.enabled is False
    log_verdict(caplog, "snapshot_reproducibility",
                {"update": "v1(sensor->light) -> v2(disabled, fan trigger)"},
                {"stored_execution_rule_version": old.rule_version,
                 "stored_snapshot_trigger": old.rule_snapshot.conditions[0].device_id,
                 "current_version": updated.version},
                "历史 execution 内嵌当时规则快照，规则更新后旧结果仍可按 v1 复现")


def test_rule_action_against_offline_target_is_recorded(container, admin, caplog):
    _devices(container, admin)
    container.rule_service.create_rule(_motion_rule(), admin)
    container.device_service.set_status("light-living", DeviceStatus.OFFLINE, admin)
    r = container.device_service.report_state(
        StateReport(device_id="sensor-hall", state={"motion": True},
                    seq=1, token="t1"), admin)
    exec_id = r.rule_execution_ids[0]
    execution = container.store.snapshot().executions[exec_id]
    assert execution.matched is True
    assert execution.action_results[0].status == "rejected"
    assert execution.action_results[0].reason == "device_offline"
    assert any("离线" in line for line in execution.trace)
    log_verdict(caplog, "rule_action_offline",
                {"event": "motion=True", "light": "offline"},
                {"action_result": execution.action_results[0].model_dump(),
                 "trace_tail": execution.trace[-1]},
                "规则匹配但目标离线：动作 rejected(device_offline)，判定依据写进 trace")
