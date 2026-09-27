"""规则引擎测试：触发、去重、重启持久化、循环依赖、版本可追溯。"""
from __future__ import annotations

import logging

from app import deps
from tests.conftest import ADMIN

log = logging.getLogger("test.rules")


def _register_devices(client):
    for d in ("sensor-1", "lamp-1"):
        r = client.post("/devices/", json={"device_id": d}, headers=ADMIN)
        assert r.status_code == 200, r.text


def _rule_body(rule_id="rule-1", value=30, actions=None):
    return {
        "rule_id": rule_id,
        "name": "高温开灯",
        "trigger": {"device_id": "sensor-1", "property": "temp",
                    "op": "gt", "value": value},
        "actions": actions if actions is not None else [
            {"device_id": "lamp-1", "action": "set",
             "params": {"power": "on"}}],
    }


def _report_temp(client, temp, version):
    return client.post("/devices/sensor-1/state", headers=ADMIN,
                       json={"device_id": "sensor-1",
                             "state": {"temp": temp},
                             "state_version": version})


def test_rule_trigger_executes_action(client):
    _register_devices(client)
    client.post("/rules/", json=_rule_body(), headers=ADMIN)
    r = _report_temp(client, 35, 1)
    assert r.status_code == 200
    lamp = client.get("/devices/lamp-1", headers=ADMIN).json()
    execs = client.get("/rules/executions", headers=ADMIN).json()
    log.info("输入: temp=35 触发阈值 30 | 关键状态: lamp=%s 执行数=%d | "
             "判定: 动作已执行且留痕", lamp["state"], len(execs))
    assert lamp["state"]["power"] == "on"
    assert len(execs) == 1
    assert execs[0]["rule_id"] == "rule-1"
    assert execs[0]["trigger_snapshot"]["changed"] == {"temp": 35}


def test_duplicate_event_not_reexecuted(client):
    _register_devices(client)
    client.post("/rules/", json=_rule_body(), headers=ADMIN)
    _report_temp(client, 35, 1)
    # 同一事件重复投递（同版本号上报会被判过期，这里直接验证执行记录数）
    _report_temp(client, 35, 1)
    execs = client.get("/rules/executions", headers=ADMIN).json()
    log.info("输入: 同一状态重复投递 | 关键状态: 执行数=%d | 判定: 不重复执行",
             len(execs))
    assert len(execs) == 1


def test_restart_does_not_reexecute(client, store_path):
    _register_devices(client)
    client.post("/rules/", json=_rule_body(), headers=ADMIN)
    _report_temp(client, 35, 1)
    before = client.get("/rules/executions", headers=ADMIN).json()

    # 模拟重启：用同一存储文件重建服务
    deps.init_services(str(store_path))
    dev = deps.device_service.get_device("sensor-1")
    execs = deps.rule_service.on_state_changed(dev, {"temp": 35})
    after = deps.rule_service.list_executions()
    log.info("输入: 重启后重放事件 %s | 关键状态: 新增执行=%d 总执行=%d | "
             "判定: 已处理事件键持久化，不重复执行",
             f"sensor-1:{dev.state_version}", len(execs), len(after))
    assert execs == []
    assert len(after) == len(before) == 1


def test_cycle_dependency_rejected(client):
    _register_devices(client)
    client.post("/rules/", json={
        "rule_id": "rule-1",
        "trigger": {"device_id": "sensor-1", "property": "a"},
        "actions": [{"device_id": "lamp-1", "action": "set",
                     "params": {"b": 1}}]}, headers=ADMIN)
    r = client.post("/rules/", json={
        "rule_id": "rule-2",
        "trigger": {"device_id": "lamp-1", "property": "b"},
        "actions": [{"device_id": "sensor-1", "action": "set",
                     "params": {"a": 1}}]}, headers=ADMIN)
    log.info("输入: rule-1 写 lamp.b, rule-2 由 lamp.b 触发并写 sensor.a | "
             "判定: 构成环 rule-1->rule-2->rule-1，拒绝")
    assert r.status_code == 422
    assert "循环依赖" in r.json()["error"]["message"]


def test_missing_device_reference_rejected(client):
    _register_devices(client)
    r = client.post("/rules/", json={
        "rule_id": "rule-1",
        "trigger": {"device_id": "ghost", "property": "x"},
        "actions": []}, headers=ADMIN)
    log.info("输入: 触发器引用不存在设备 ghost | 判定: 422 并指明缺失设备")
    assert r.status_code == 422
    assert "ghost" in r.json()["error"]["message"]


def test_update_keeps_history_reproducible(client):
    _register_devices(client)
    client.post("/rules/", json=_rule_body(), headers=ADMIN)
    _report_temp(client, 35, 1)
    # 更新规则：阈值 30 -> 40，revision 递增
    r = client.put("/rules/rule-1", json=_rule_body(value=40), headers=ADMIN)
    assert r.json()["revision"] == 2
    execs = client.get("/rules/executions", headers=ADMIN).json()
    log.info("输入: 更新规则阈值 | 关键状态: 历史执行 revision=%d 当前 revision=2 | "
             "判定: 历史记录保留旧版本定义可复现", execs[0]["rule_revision"])
    assert execs[0]["rule_revision"] == 1
    assert execs[0]["trigger_snapshot"]["state"]["temp"] == 35
    # 新阈值生效：temp=35 不再触发
    _report_temp(client, 36, 2)
    assert len(client.get("/rules/executions", headers=ADMIN).json()) == 1
    _report_temp(client, 41, 3)
    assert len(client.get("/rules/executions", headers=ADMIN).json()) == 2


def test_disabled_rule_does_not_fire(client):
    _register_devices(client)
    client.post("/rules/", json=_rule_body(), headers=ADMIN)
    client.post("/rules/rule-1/enabled", json={"enabled": False}, headers=ADMIN)
    _report_temp(client, 35, 1)
    execs = client.get("/rules/executions", headers=ADMIN).json()
    log.info("输入: 停用规则后上报 temp=35 | 关键状态: 执行数=%d | 判定: 不触发",
             len(execs))
    assert execs == []
