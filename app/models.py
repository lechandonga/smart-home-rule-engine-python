"""Domain model types.

These dataclasses/enums are the single source of truth for the shape of
devices, commands, rules and audit records. Persistence (store.py) and the
HTTP layer both depend on them, never the other way around.
"""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class DeviceType(str, enum.Enum):
    LIGHT = "light"
    SWITCH = "switch"
    THERMOSTAT = "thermostat"
    SENSOR = "sensor"
    LOCK = "lock"


class DeviceStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"


# Allowed state properties per device type: property -> (python type, bounds/choices)
# A reported state may contain a subset of the declared properties.
DEVICE_STATE_SPEC: dict[DeviceType, dict[str, dict[str, Any]]] = {
    DeviceType.LIGHT: {
        "power": {"type": "bool"},
        "brightness": {"type": "int", "min": 0, "max": 100},
    },
    DeviceType.SWITCH: {
        "power": {"type": "bool"},
    },
    DeviceType.THERMOSTAT: {
        "power": {"type": "bool"},
        "target_temp": {"type": "float", "min": 10.0, "max": 35.0},
        "mode": {"type": "enum", "choices": ("heat", "cool", "auto")},
    },
    DeviceType.SENSOR: {
        "temperature": {"type": "float", "min": -40.0, "max": 85.0},
        "humidity": {"type": "float", "min": 0.0, "max": 100.0},
        "motion": {"type": "bool"},
    },
    DeviceType.LOCK: {
        "locked": {"type": "bool"},
    },
}


TriggerOperator = Literal["eq", "ne", "gt", "ge", "lt", "le"]


class Device(BaseModel):
    device_id: str
    name: str
    type: DeviceType
    owner: str
    status: DeviceStatus = DeviceStatus.ONLINE
    state: dict[str, Any] = Field(default_factory=dict)
    """Monotonic per-device state version, bumped every time state changes."""
    version: int = 0
    """Highest accepted client report sequence number (None before first report)."""
    last_report_seq: int | None = None
    last_report_token: str | None = None
    updated_at: str


class RegisterDeviceRequest(BaseModel):
    device_id: str
    name: str
    type: DeviceType
    initial_state: dict[str, Any] = Field(default_factory=dict)


class StateReport(BaseModel):
    """State report envelope.

    ``seq`` is a per-device monotonically increasing number chosen by the
    (simulated) device; older seq values are stale. ``token`` is the report
    idempotency key for safe retries of the same request.
    """

    device_id: str
    state: dict[str, Any]
    seq: int = Field(ge=0)
    token: str = Field(min_length=1)
    reported_at: str | None = None


CommandStatus = Literal["applied", "rejected"]


class CommandRequest(BaseModel):
    command_id: str = Field(min_length=1)
    device_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class CommandRecord(BaseModel):
    command_id: str
    device_id: str
    params: dict[str, Any]
    status: CommandStatus
    reason: str | None = None
    caller_id: str
    """``None`` when submitted through the API, otherwise the rule id."""
    source_rule_id: str | None = None
    executed_at: str


class CommandResult(BaseModel):
    """``duplicate=True`` means a command with the same id was already processed;
    the original (deterministic) result is echoed back."""

    duplicate: bool
    command: CommandRecord


class ReportResult(BaseModel):
    accepted: bool
    reason: Literal["duplicate", "stale", "invalid_state", None] = None
    device: Device
    """Ids of rule executions produced by this report (if any)."""
    rule_execution_ids: list[str] = Field(default_factory=list)


class Condition(BaseModel):
    device_id: str
    property: str = Field(min_length=1)
    op: TriggerOperator
    value: Any


class Action(BaseModel):
    device_id: str
    params: dict[str, Any] = Field(default_factory=dict)


class Rule(BaseModel):
    rule_id: str
    name: str
    enabled: bool = True
    conditions: list[Condition]
    actions: list[Action]
    version: int = 1
    created_by: str
    created_at: str
    updated_at: str


class RuleCreate(BaseModel):
    rule_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    enabled: bool = True
    conditions: list[Condition] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)


class RuleUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    conditions: list[Condition] | None = None
    actions: list[Action] | None = None


class ActionResult(BaseModel):
    device_id: str
    params: dict[str, Any]
    status: CommandStatus
    reason: str | None = None


class RuleExecution(BaseModel):
    """Immutable record of one rule evaluation, kept forever for replay/trace."""

    execution_id: str
    event_key: str
    rule_id: str
    rule_version: int
    rule_snapshot: Rule
    triggered_at: str
    event_device_id: str
    event_seq: int
    event_state: dict[str, Any]
    matched: bool
    status: Literal["executed", "skipped_disabled", "failed_no_match"]
    action_results: list[ActionResult] = Field(default_factory=list)
    trace: list[str] = Field(default_factory=list)


class AuditEntry(BaseModel):
    audit_id: str
    ts: str
    caller_id: str
    action: str
    resource_type: str
    resource_id: str
    result: Literal["allowed", "denied"]
    reason: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class Caller(BaseModel):
    caller_id: str
    name: str
    role: Literal["admin", "user"] = "user"
    """Device ids this caller may operate. Ignored for role=admin (all devices)."""
    allowed_devices: list[str] = Field(default_factory=list)
