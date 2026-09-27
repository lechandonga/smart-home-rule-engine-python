"""Authorization boundary: callers may only touch granted devices/rules."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.models import RegisterDeviceRequest, RuleCreate, Condition, Action
from tests.conftest import headers, log_verdict


def _seed(client: TestClient) -> None:
    client.post("/devices", headers=headers("admin"), json={
        "device_id": "light-living", "name": "light", "type": "light",
        "initial_state": {"power": False}})
    client.post("/devices", headers=headers("alice"), json={
        "device_id": "sensor-hall", "name": "sensor", "type": "sensor"})
    client.post("/devices", headers=headers("bob"), json={
        "device_id": "lock-door", "name": "lock", "type": "lock",
        "initial_state": {"locked": True}})
    client.post("/rules", headers=headers("alice"), json={
        "rule_id": "alice-rule", "name": "a",
        "conditions": [{"device_id": "sensor-hall", "property": "motion",
                        "op": "eq", "value": True}],
        "actions": [{"device_id": "light-living", "params": {"power": True}}]})


def test_device_access_is_scoped_and_denied_is_audited(client, caplog):
    _seed(client)
    # alice may read her own devices; bob may not touch alice's light.
    ok = client.get("/devices/light-living", headers=headers("alice"))
    denied = client.get("/devices/light-living", headers=headers("bob"))
    cmd = client.post("/devices/light-living/commands", headers=headers("bob"),
                      json={"command_id": "x", "device_id": "light-living",
                            "params": {"power": True}})
    report = client.post("/devices/light-living/reports", headers=headers("bob"),
                         json={"state": {"power": True}, "seq": 0, "token": "z"})
    assert ok.status_code == 200
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "forbidden"
    assert cmd.status_code == 403
    assert report.status_code == 403

    audit = client.get("/audit", headers=headers("admin")).json()
    bob_denials = [a for a in audit if a["caller_id"] == "bob" and a["result"] == "denied"]
    assert len(bob_denials) >= 3
    assert {a["action"] for a in bob_denials} >= {"get_device", "send_command", "report_state"}
    # The forbidden command must not have executed.
    assert "x" not in client.app.state.container.store.snapshot().commands
    log_verdict(caplog, "device_scope",
                {"caller": "bob", "allowed_devices": ["lock-door"],
                 "target": "light-living"},
                {"denied_status": denied.status_code,
                 "denied_actions": sorted({a["action"] for a in bob_denials})},
                "越权读/写/上报全部 403，审计留下 denied 记录，命令不落库")


def test_rule_ownership_boundary(client, caplog):
    _seed(client)
    # bob cannot view or modify alice's rule.
    view = client.get("/rules/alice-rule", headers=headers("bob"))
    upd = client.put("/rules/alice-rule", headers=headers("bob"),
                     json={"enabled": False})
    lst = client.get("/rules", headers=headers("bob")).json()
    assert view.status_code == 403 and upd.status_code == 403
    assert [r["rule_id"] for r in lst] == []

    # alice cannot create a rule driving bob's device.
    cross = client.post("/rules", headers=headers("alice"), json={
        "rule_id": "cross-rule", "name": "c",
        "conditions": [{"device_id": "sensor-hall", "property": "motion",
                        "op": "eq", "value": True}],
        "actions": [{"device_id": "lock-door", "params": {"locked": False}}]})
    assert cross.status_code == 403
    audit = client.get("/audit", headers=headers("admin")).json()
    denials = [a for a in audit if a["result"] == "denied"]
    assert any(a["action"] == "get_rule" for a in denials)
    assert any(a["action"] == "update_rule" for a in denials)
    log_verdict(caplog, "rule_ownership",
                {"caller": "bob/bob-vs-alice-rule", "cross_target": "lock-door"},
                {"view": view.status_code, "update": upd.status_code,
                 "cross_rule_create": cross.status_code},
                "非创建者不能查看/修改规则；规则动作不得越权驱动他人设备；均审计")


def test_unknown_caller_and_missing_header_rejected(client):
    assert client.get("/devices").status_code == 401
    r = client.get("/devices", headers=headers("nobody"))
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
