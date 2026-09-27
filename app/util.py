"""Small shared helpers (time, ids, validation messages)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from app.errors import ValidationFailed
from app.models import DEVICE_STATE_SPEC, DeviceType


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def describe_state(spec_rule: dict[str, Any]) -> str:
    kind = spec_rule["type"]
    if kind in ("int", "float"):
        return f"{kind} in [{spec_rule['min']}, {spec_rule['max']}]"
    if kind == "enum":
        return f"one of {list(spec_rule['choices'])}"
    return kind


def validate_state(type_: DeviceType, state: dict[str, Any]) -> None:
    """Validate a state (or command params, same spec) for a device type."""
    spec = DEVICE_STATE_SPEC[type_]
    if not isinstance(state, dict):
        raise ValidationFailed("状态必须是对象", details={"device_type": type_.value})
    for key, value in state.items():
        if key not in spec:
            raise ValidationFailed(
                f"设备类型 {type_.value} 不支持字段 {key!r}",
                details={"allowed_properties": list(spec), "unknown": key},
            )
        rule = spec[key]
        kind = rule["type"]
        if kind == "bool":
            ok = isinstance(value, bool)
        elif kind == "int":
            ok = isinstance(value, int) and not isinstance(value, bool)
            if ok:
                ok = rule["min"] <= value <= rule["max"]
        elif kind == "float":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
            if ok:
                ok = rule["min"] <= value <= rule["max"]
        else:  # enum
            ok = value in rule["choices"]
        if not ok:
            raise ValidationFailed(
                f"字段 {key!r} 的值 {value!r} 不合法，要求 {describe_state(rule)}",
                details={"property": key, "value": value, "expected": describe_state(rule)},
            )
