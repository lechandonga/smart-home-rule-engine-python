"""Local JSON-file persistence (the simulated local data source).

Concurrency model
-----------------
Every mutation runs inside :meth:`Store.mutate` while holding one RLock, so
concurrent state reports / commands are serialized into a single total order:
the result is deterministic and easy to explain. After each mutation the whole
state is atomically rewritten (temp file + ``os.replace``), so a crash never
leaves a half-written file.

Versioning
----------
``schema_version`` is stored inside the data file. Old data is migrated
forward through well-defined steps. Unknown/newer versions and data that
cannot be safely migrated are rejected with :class:`DataMigrationError`
carrying the precise reason, never silently coerced.

v1 -> v2 semantics
~~~~~~~~~~~~~~~~~~
v1 devices used ``online: bool`` instead of ``status: "online"/"offline"`` and
had no sequence tracking; migrated devices start at version 0 with no accepted
report seq. v1 rules used a single ``trigger{device,property,equals}`` plus
``target{device,set}``; they become one ``eq`` condition and one action.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

from app.errors import DataMigrationError
from app.models import (
    DEVICE_STATE_SPEC,
    CommandRecord,
    Condition,
    Action,
    Device,
    DeviceStatus,
    DeviceType,
    Rule,
    RuleExecution,
)

T = TypeVar("T")

LEGACY_EPOCH = "1970-01-01T00:00:00Z"


def _validate_state_against_spec(type_: DeviceType, state: dict[str, Any], *, where: str) -> None:
    """Validate a state dict for a device type. Raises DataMigrationError on bad data."""
    spec = DEVICE_STATE_SPEC[type_]
    for key, value in state.items():
        if key not in spec:
            raise DataMigrationError(
                f"{where}: 未知状态字段 {key!r}",
                details={"device_type": type_.value, "unknown_property": key},
            )
        rule = spec[key]
        kind = rule["type"]
        ok = (
            (kind == "bool" and isinstance(value, bool))
            or (kind == "int" and isinstance(value, int) and not isinstance(value, bool))
            or (kind == "float" and isinstance(value, (int, float)) and not isinstance(value, bool))
            or (kind == "enum" and value in rule["choices"])
        )
        if kind in ("int", "float") and ok:
            lo, hi = rule["min"], rule["max"]
            ok = lo <= value <= hi
        if not ok:
            raise DataMigrationError(
                f"{where}: 状态字段 {key!r} 的值 {value!r} 不合法",
                details={"property": key, "value": value, "expected": rule},
            )


class State:
    """Mutable in-memory view handed to ``mutate`` callbacks."""

    def __init__(self) -> None:
        self.devices: dict[str, Device] = {}
        self.commands: dict[str, CommandRecord] = {}
        self.rules: dict[str, Rule] = {}
        self.executions: dict[str, RuleExecution] = {}
        # (device_id, seq) -> execution_id  (exactly-once event processing)
        self.processed_events: dict[str, str] = {}
        # (device_id, token) -> {"seq": int, "execution_ids": [...]}
        self.report_tokens: dict[str, dict[str, Any]] = {}

    def to_raw(self, schema_version: int) -> dict[str, Any]:
        return {
            "schema_version": schema_version,
            "devices": [d.model_dump() for d in self.devices.values()],
            "commands": [c.model_dump() for c in self.commands.values()],
            "rules": [r.model_dump() for r in self.rules.values()],
            "executions": [e.model_dump() for e in self.executions.values()],
            "processed_events": self.processed_events,
            "report_tokens": self.report_tokens,
        }


def _migrate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or "schema_version" not in raw:
        raise DataMigrationError("数据文件缺少 schema_version，无法判断数据版本")
    try:
        version = int(raw["schema_version"])
    except (TypeError, ValueError):
        raise DataMigrationError(
            "schema_version 不是整数", details={"value": raw["schema_version"]}
        )
    if version > Store.CURRENT_SCHEMA_VERSION:
        raise DataMigrationError(
            f"数据版本 v{version} 比当前软件支持的最高版本 v{Store.CURRENT_SCHEMA_VERSION} 更新，"
            "拒绝在旧版本软件上读取（可能造成语义丢失）",
            details={"found": version, "supported_max": Store.CURRENT_SCHEMA_VERSION},
        )
    if version < 1:
        raise DataMigrationError(f"不支持的数据版本 v{version}")

    if version == 1:
        raw = _migrate_v1_to_v2(raw)
    return raw


def _migrate_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    devices: list[dict[str, Any]] = []
    for d in raw.get("devices", []):
        where = f"迁移 v1 设备 {d.get('device_id')!r}"
        if not isinstance(d, dict) or "device_id" not in d or "type" not in d:
            raise DataMigrationError(f"{where}: 缺少 device_id/type")
        try:
            dtype = DeviceType(d["type"])
        except ValueError:
            raise DataMigrationError(
                f"{where}: 未知设备类型 {d['type']!r}", details={"bad_type": d["type"]}
            )
        state = d.get("state", {})
        if not isinstance(state, dict):
            raise DataMigrationError(f"{where}: state 必须是对象")
        _validate_state_against_spec(dtype, state, where=where)
        devices.append(
            Device(
                device_id=d["device_id"],
                name=d.get("name", d["device_id"]),
                type=dtype,
                owner=d.get("owner", "legacy"),
                status=DeviceStatus.ONLINE if d.get("online", True) else DeviceStatus.OFFLINE,
                state=state,
                version=0,
                last_report_seq=None,
                last_report_token=None,
                updated_at=d.get("updated_at", LEGACY_EPOCH),
            ).model_dump()
        )

    rules: list[dict[str, Any]] = []
    for r in raw.get("rules", []):
        where = f"迁移 v1 规则 {r.get('rule_id')!r}"
        if not isinstance(r, dict) or "rule_id" not in r:
            raise DataMigrationError(f"{where}: 缺少 rule_id")
        trig = r.get("trigger") or {}
        target = r.get("target") or {}
        for obj, name in ((trig, "trigger"), (target, "target")):
            if not isinstance(obj, dict) or "device_id" not in obj:
                raise DataMigrationError(f"{where}: {name} 结构不完整")
        rules.append(
            Rule(
                rule_id=r["rule_id"],
                name=r.get("title", r["rule_id"]),
                enabled=bool(r.get("active", True)),
                conditions=[
                    Condition(
                        device_id=trig["device_id"],
                        property=trig.get("property", ""),
                        op="eq",
                        value=trig.get("equals"),
                    ).model_dump()
                ],
                actions=[
                    Action(
                        device_id=target["device_id"],
                        params=dict(target.get("set", {})),
                    ).model_dump()
                ],
                version=1,
                created_by="legacy-migration",
                created_at=r.get("created_at", LEGACY_EPOCH),
                updated_at=r.get("updated_at", LEGACY_EPOCH),
            ).model_dump()
        )

    return {
        "schema_version": 2,
        "devices": devices,
        "commands": list(raw.get("commands", [])),
        "rules": rules,
        "executions": [],
        "processed_events": {},
        "report_tokens": {},
    }


class Store:
    CURRENT_SCHEMA_VERSION = 2

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self.state = State()
        self._load()

    # ------------------------------------------------------------------ I/O

    def _load(self) -> None:
        if not self.path.exists():
            # Empty start; the file is created on the first mutation rather
            # than at construction time, so importing the app has no FS side
            # effects (and tests using temp dirs never touch the repo data/).
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw = _migrate(raw)
        st = State()
        st.devices = {d["device_id"]: Device(**d) for d in raw.get("devices", [])}
        st.commands = {c["command_id"]: CommandRecord(**c) for c in raw.get("commands", [])}
        st.rules = {r["rule_id"]: Rule(**r) for r in raw.get("rules", [])}
        st.executions = {
            e["execution_id"]: RuleExecution(**e) for e in raw.get("executions", [])
        }
        st.processed_events = dict(raw.get("processed_events", {}))
        st.report_tokens = dict(raw.get("report_tokens", {}))
        self.state = st

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            self.state.to_raw(self.CURRENT_SCHEMA_VERSION), ensure_ascii=False, indent=2
        )
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ------------------------------------------------------------ concurrency

    def mutate(self, fn: Callable[[State], T]) -> T:
        """Run ``fn`` against mutable state under the global write lock."""
        with self._lock:
            result = fn(self.state)
            self._persist_locked()
            return result

    def snapshot(self) -> State:
        """Return a deep, isolated copy of the current state for reads."""
        with self._lock:
            return copy.deepcopy(self.state)
