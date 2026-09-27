"""权限边界测试：未认证、越权设备/规则访问、审计留痕。"""
from __future__ import annotations

import logging

from tests.conftest import ADMIN, ALICE, BOB

log = logging.getLogger("test.auth")


def _setup(client):
    for d in ("lamp-1", "lamp-2"):
        client.post("/devices/", json={"device_id": d}, headers=ADMIN)
    client.post("/rules/", json={
        "rule_id": "rule-1",
        "trigger": {"device_id": "lamp-1", "property": "power"},
        "actions": []}, headers=ADMIN)


def test_missing_or_bad_api_key(client):
    r1 = client.get("/devices/")
    r2 = client.get("/devices/", headers={"X-Api-Key": "nope"})
    log.info("输入: 无 key / 错误 key | 判定: 均 401")
    assert r1.status_code == 401 and r2.status_code == 401


def test_device_scope_enforced_and_audited(client):
    _setup(client)
    ok = client.get("/devices/lamp-1", headers=ALICE)
    denied = client.get("/devices/lamp-2", headers=ALICE)
    log.info("输入: alice 访问 lamp-1(授权内) 与 lamp-2(授权外) | "
             "判定: 200 / 403 且留审计")
    assert ok.status_code == 200
    assert denied.status_code == 403
    audit = client.get("/audit/", headers=ADMIN).json()
    denies = [a for a in audit if a["decision"] == "deny"
              and a["target"] == "lamp-2" and a["caller"] == "alice"]
    assert denies, "越权访问未留审计记录"


def test_command_scope_enforced(client):
    _setup(client)
    r = client.post("/devices/commands", headers=BOB,
                    json={"device_id": "lamp-1", "action": "set",
                          "params": {"power": "on"}})
    log.info("输入: bob 向 lamp-1 发命令(授权外) | 判定: 403")
    assert r.status_code == 403


def test_rule_scope_enforced_and_list_filtered(client):
    _setup(client)
    denied = client.get("/rules/rule-1", headers=BOB)
    assert denied.status_code == 403
    alice_rules = client.get("/rules/", headers=ALICE).json()
    bob_rules = client.get("/rules/", headers=BOB).json()
    log.info("输入: alice/bob 各列规则 | 关键状态: alice=%d bob=%d | "
             "判定: 列表按授权范围过滤", len(alice_rules), len(bob_rules))
    assert len(alice_rules) == 1 and bob_rules == []


def test_non_admin_cannot_register_or_view_audit(client):
    r1 = client.post("/devices/", json={"device_id": "x"}, headers=ALICE)
    r2 = client.get("/audit/", headers=ALICE)
    log.info("输入: alice 注册设备 / 查看审计 | 判定: 均 403")
    assert r1.status_code == 403 and r2.status_code == 403
