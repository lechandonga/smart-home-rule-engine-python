"""旧版本数据兼容测试：v1 自动迁移、过旧/过新版本拒绝。"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import deps
from app.errors import MigrationError
from app.migrations import migrate
from app.storage import Storage

log = logging.getLogger("test.migration")


def test_v1_data_migrates_with_stable_semantics(tmp_path):
    v1 = {
        "devices": {"lamp-1": {"device_id": "lamp-1",
                               "state": {"power": "on"},
                               "state_version": 3}},
        "rules": {"rule-1": {
            "rule_id": "rule-1",
            "trigger": {"device_id": "lamp-1", "property": "power"},
            "actions": []}},
        "commands": {}, "executions": {},
    }
    path = tmp_path / "store.json"
    path.write_text(json.dumps(v1))
    st = Storage(path)
    st.load()
    dev = st.data.devices["lamp-1"]
    rule = st.data.rules["rule-1"]
    log.info("输入: v1 数据(无版本号) | 关键状态: dev.version=%d rule.revision=%d "
             "schema=%d | 判定: 补默认值迁移，原字段语义不变",
             dev.state_version, rule.revision, st.data.schema_version)
    assert st.data.schema_version == 2
    assert dev.state_version == 3 and dev.state == {"power": "on"}
    assert dev.allowed_properties is None
    assert rule.revision == 1


def test_too_old_version_rejected():
    with pytest.raises(MigrationError) as exc:
        migrate({"schema_version": 0})
    log.info("输入: schema_version=0 | 判定: 拒绝并说明原因: %s", exc.value.message)
    assert "过旧" in exc.value.message


def test_future_version_rejected(tmp_path):
    path = tmp_path / "store.json"
    path.write_text(json.dumps({"schema_version": 99, "devices": {}}))
    st = Storage(path)
    with pytest.raises(MigrationError) as exc:
        st.load()
    log.info("输入: schema_version=99 | 判定: 拒绝降级: %s", exc.value.message)
    assert "高于当前支持" in exc.value.message


def test_service_boots_on_migrated_data(tmp_path):
    """迁移后的数据能被完整服务栈加载使用。"""
    path = tmp_path / "store.json"
    path.write_text(json.dumps({
        "devices": {"lamp-1": {"device_id": "lamp-1"}},
        "commands": {}, "rules": {}, "executions": {}}))
    deps.init_services(str(path))
    from main import app
    with TestClient(app) as c:
        r = c.get("/devices/lamp-1", headers={"X-Api-Key": "admin-key"})
    log.info("输入: 迁移后启动服务查询 lamp-1 | 判定: 200")
    assert r.status_code == 200
