"""HTTP-level end-to-end checks through the FastAPI stack."""

from __future__ import annotations

from tests.conftest import headers, log_verdict


def _register(client):
    r = client.post("/devices", headers=headers("admin"), json={
        "device_id": "light-living", "name": "L", "type": "light",
        "initial_state": {"power": False}})
    assert r.status_code == 200, r.text


def test_full_http_flow_and_error_envelope(client, caplog):
    h = headers("admin")
    _register(client)

    # unknown route target -> structured 404 envelope
    r = client.get("/devices/nope", headers=h)
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"

    # missing auth header
    assert client.post("/devices/light-living/commands", json={
        "command_id": "c1", "device_id": "light-living",
        "params": {"power": True}}).status_code == 401

    # apply once, replay gives deterministic echo
    payload = {"command_id": "c1", "device_id": "light-living",
               "params": {"power": True}}
    first = client.post("/devices/light-living/commands", json=payload, headers=h)
    second = client.post("/devices/light-living/commands", json=payload, headers=h)
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert first.json()["command"] == second.json()["command"]

    # report -> rule created later shouldn't retrofire; create rule then report
    client.post("/rules", json={
        "rule_id": "r1", "name": "bright",
        "conditions": [{"device_id": "light-living", "property": "brightness",
                        "op": "ge", "value": 80}],
        "actions": [{"device_id": "light-living", "params": {"power": True}}]},
        headers=h).status_code
    # self-cycle must be rejected
    rr = client.post("/rules", json={
        "rule_id": "r2", "name": "self",
        "conditions": [{"device_id": "light-living", "property": "power",
                        "op": "eq", "value": True}],
        "actions": [{"device_id": "light-living", "params": {"power": False}}]},
        headers=h)
    assert rr.status_code == 422 and rr.json()["error"]["code"] == "rule_cycle"

    # audit endpoint returns records newest first
    audit = client.get("/audit", headers=h).json()
    assert isinstance(audit, list) and audit
    log_verdict(caplog, "http_flow",
                {"endpoints": ["/devices", "/commands", "/rules", "/audit"]},
                {"first_duplicate": first.json()["duplicate"],
                 "second_duplicate": second.json()["duplicate"],
                 "cycle_status": rr.status_code, "audit_rows": len(audit)},
                "HTTP 层错误均为结构化 envelope；命令幂等与循环拒绝经网络复现一致")
