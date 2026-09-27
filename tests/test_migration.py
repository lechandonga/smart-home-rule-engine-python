"""Legacy data compatibility: v1 -> v2 migration and unsafe-data rejection."""

from __future__ import annotations

import json

from app.container import Container
from app.errors import DataMigrationError
from tests.conftest import log_verdict


def _write_state(data_dir, raw: dict) -> None:
    (data_dir / "state.json").write_text(json.dumps(raw), encoding="utf-8")


def test_v1_state_and_rules_migrate_with_stable_semantics(data_dir, caplog):
    _write_state(data_dir, {
        "schema_version": 1,
        "devices": [
            {"device_id": "light-living", "type": "light", "title_ignored": True,
             "online": False, "state": {"power": True, "brightness": 40}},
            {"device_id": "sensor-hall", "type": "sensor", "online": True,
             "state": {"temperature": 21.5}},
        ],
        "rules": [
            {"rule_id": "legacy-1", "title": "old motion rule", "active": False,
             "trigger": {"device_id": "sensor-hall", "property": "motion",
                         "equals": True},
             "target": {"device_id": "light-living", "set": {"power": True}}},
        ],
    })
    c = Container(data_dir)
    snap = c.store.snapshot()
    light = snap.devices["light-living"]
    rule = snap.rules["legacy-1"]

    assert light.status.value == "offline"          # online:false mapping
    assert light.version == 0 and light.last_report_seq is None
    assert light.state == {"power": True, "brightness": 40}
    assert rule.conditions[0].op == "eq"
    assert rule.conditions[0].value is True
    assert rule.actions[0].device_id == "light-living"
    assert rule.enabled is False and rule.created_by == "legacy-migration"
    assert snap.report_tokens == {} and snap.processed_events == {}
    # File is rewritten at current schema after load-on-mutate.
    c.store.mutate(lambda st: None)
    persisted = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
    assert persisted["schema_version"] == 2
    log_verdict(caplog, "v1_to_v2",
                {"v1": "online:bool + trigger{equals}/target{set}"},
                {"status": light.status.value, "rule_op": rule.conditions[0].op,
                 "rule_enabled": rule.enabled,
                 "persisted_schema": persisted["schema_version"]},
                "online:false->status:offline；trigger->eq 条件；active:false->enabled:false；迁移后落 v2")


def test_newer_schema_is_refused(data_dir, caplog):
    _write_state(data_dir, {"schema_version": 99, "devices": [], "rules": []})
    try:
        Container(data_dir)
        assert False
    except DataMigrationError as e:
        assert e.code == "data_migration_failed"
        assert e.details["found"] == 99
    log_verdict(caplog, "newer_schema", {"schema_version": 99},
                {"error_code": "data_migration_failed"},
                "前向兼容不成立时拒绝启动并说明支持上限，绝不静默降级读取")


def test_v1_unknown_device_type_refused(data_dir, caplog):
    _write_state(data_dir, {
        "schema_version": 1,
        "devices": [{"device_id": "x", "type": "teleporter", "online": True,
                     "state": {}}],
        "rules": [],
    })
    try:
        Container(data_dir)
        assert False
    except DataMigrationError as e:
        reason = e.message
    assert "teleporter" in reason
    log_verdict(caplog, "v1_bad_type", {"type": "teleporter"},
                {"reason": reason}, "无法安全迁移的未知设备类型被拒绝并说明原因")


def test_v1_invalid_state_refused(data_dir, caplog):
    _write_state(data_dir, {
        "schema_version": 1,
        "devices": [{"device_id": "x", "type": "light", "online": True,
                     "state": {"brightness": 250}}],
        "rules": [],
    })
    try:
        Container(data_dir)
        assert False
    except DataMigrationError as e:
        reason = e.message
    assert "brightness" in reason
    log_verdict(caplog, "v1_invalid_state", {"brightness": 250},
                {"reason": reason}, "超出稳定语义范围的旧状态值被拒绝，不做猜测性截断")
