"""存储数据版本迁移。

语义约定：
- schema_version 缺失时视为 1（最早版本，无显式版本号）。
- v1 -> v2：
  * 设备记录补充 allowed_properties=None、registered_at/updated_at 缺省值；
  * 规则记录补充 revision=1；
  * 新增 processed_events / audit_seq 字段，默认空。
  以上均为"补充默认值"型迁移，语义稳定：旧字段含义不变。
- 版本 < 1 或 > 当前版本：无法安全解释，直接拒绝（MigrationError）。
"""
from __future__ import annotations

from .errors import MigrationError
from .models import STORAGE_SCHEMA_VERSION

MIN_SUPPORTED_VERSION = 1


def _migrate_1_to_2(raw: dict) -> dict:
    data = dict(raw)
    for dev in data.get("devices", {}).values():
        dev.setdefault("allowed_properties", None)
        dev.setdefault("state_version", 0)
    for rule in data.get("rules", {}).values():
        rule.setdefault("revision", 1)
    data.setdefault("processed_events", [])
    data.setdefault("audit", [])
    data.setdefault("audit_seq", 0)
    data["schema_version"] = 2
    return data


_MIGRATIONS = {1: _migrate_1_to_2}


def migrate(raw: dict) -> dict:
    version = raw.get("schema_version", 1)
    if not isinstance(version, int):
        raise MigrationError(
            f"schema_version 非法: {version!r}",
            detail={"schema_version": repr(version)},
        )
    if version < MIN_SUPPORTED_VERSION:
        raise MigrationError(
            f"数据版本 {version} 过旧，最低支持 {MIN_SUPPORTED_VERSION}，拒绝加载",
            detail={"schema_version": version},
        )
    if version > STORAGE_SCHEMA_VERSION:
        raise MigrationError(
            f"数据版本 {version} 高于当前支持的 {STORAGE_SCHEMA_VERSION}，"
            "无法安全降级，拒绝加载",
            detail={"schema_version": version},
        )
    data = raw
    while version < STORAGE_SCHEMA_VERSION:
        data = _MIGRATIONS[version](data)
        version = data["schema_version"]
    return data
