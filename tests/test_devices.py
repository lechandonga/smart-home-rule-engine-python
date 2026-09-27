"""设备状态与命令语义测试：并发上报、重复命令、离线设备、非法/过期数据。"""
from __future__ import annotations

import logging
import random
from concurrent.futures import ThreadPoolExecutor

from tests.conftest import ADMIN

log = logging.getLogger("test.devices")


def _register(client, device_id="lamp-1", allowed=None):
    body = {"device_id": device_id, "name": device_id,
            "allowed_properties": allowed}
    r = client.post("/devices/", json=body, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def test_register_is_idempotent(client):
    _register(client)
    first = client.get("/devices/lamp-1", headers=ADMIN).json()
    _register(client)  # 重复注册不覆盖
    second = client.get("/devices/lamp-1", headers=ADMIN).json()
    log.info("输入: 重复注册 lamp-1 | 关键状态: state_version=%d/%d | 判定: 两次读取一致",
             first["state_version"], second["state_version"])
    assert first == second


def test_stale_report_rejected(client):
    _register(client)
    r1 = client.post("/devices/lamp-1/state", headers=ADMIN,
                     json={"device_id": "lamp-1", "state": {"power": "on"},
                           "state_version": 5})
    assert r1.status_code == 200
    r2 = client.post("/devices/lamp-1/state", headers=ADMIN,
                     json={"device_id": "lamp-1", "state": {"power": "off"},
                           "state_version": 5})
    log.info("输入: 重复版本号 5 上报 | 关键状态: 当前版本 5 | 判定: 过期数据拒绝")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "stale_data"


def test_invalid_state_rejected(client):
    _register(client, allowed={"power": ["on", "off"]})
    r = client.post("/devices/lamp-1/state", headers=ADMIN,
                    json={"device_id": "lamp-1", "state": {"power": "sideways"},
                          "state_version": 1})
    log.info("输入: 非法取值 sideways | 判定: 422 validation_failed")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


def test_concurrent_reports_deterministic(client):
    """并发上报：无论到达顺序，最终状态版本 = 最大版本号。"""
    _register(client)
    versions = list(range(1, 21))
    random.Random(42).shuffle(versions)

    def report(v):
        return client.post("/devices/lamp-1/state", headers=ADMIN,
                           json={"device_id": "lamp-1",
                                 "state": {"v": v}, "state_version": v})

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(report, versions))

    codes = [r.status_code for r in responses]
    dev = client.get("/devices/lamp-1", headers=ADMIN).json()
    log.info("输入: 并发版本 %s | 关键状态: 接受=%d 拒绝=%d 最终版本=%d | "
             "判定: 最终版本必须等于最大输入版本 20",
             versions, codes.count(200), codes.count(409), dev["state_version"])
    assert dev["state_version"] == 20
    assert dev["state"]["v"] == 20
    assert codes.count(200) + codes.count(409) == 20


def test_duplicate_command_idempotent(client):
    _register(client)
    body = {"device_id": "lamp-1", "action": "set",
            "params": {"power": "on"}, "idempotency_key": "req-1"}

    def send():
        return client.post("/devices/commands", json=body, headers=ADMIN)

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: send(), range(6)))

    ids = {r.json()["command_id"] for r in responses}
    log.info("输入: 同一幂等键并发提交 6 次 | 关键状态: 去重后命令数=%d | "
             "判定: 全部返回同一 command_id", len(ids))
    assert len(ids) == 1
    assert all(r.json()["status"] == "executed" for r in responses)


def test_offline_device_command_rejected(client):
    _register(client)
    client.post("/devices/lamp-1/status", json={"online": False}, headers=ADMIN)
    r = client.post("/devices/commands", headers=ADMIN,
                    json={"device_id": "lamp-1", "action": "set",
                          "params": {"power": "on"}})
    cmd = r.json()
    log.info("输入: 离线设备下发命令 | 关键状态: status=%s reason=%s | "
             "判定: 确定性拒绝且留痕", cmd["status"], cmd["reason"])
    assert cmd["status"] == "rejected"
    assert cmd["reason"] == "device_offline"
    # 结果可查询、可解释
    again = client.get(f"/devices/commands/{cmd['command_id']}", headers=ADMIN)
    assert again.json()["reason"] == "device_offline"


def test_command_set_updates_state_and_bumps_version(client):
    _register(client)
    r = client.post("/devices/commands", headers=ADMIN,
                    json={"device_id": "lamp-1", "action": "set",
                          "params": {"power": "on"}})
    assert r.json()["status"] == "executed"
    dev = client.get("/devices/lamp-1", headers=ADMIN).json()
    log.info("输入: set power=on | 关键状态: state=%s version=%d",
             dev["state"], dev["state_version"])
    assert dev["state"]["power"] == "on"
    assert dev["state_version"] == 1
