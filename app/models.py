"""领域模型与枚举定义。

只包含类型定义，不包含业务逻辑。
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------

class DeviceStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"


class Device(BaseModel):
    device_id: str
    name: str = ""
    status: DeviceStatus = DeviceStatus.ONLINE
    # 状态属性，例如 {"power": "on", "brightness": 80}
    state: dict[str, Any] = Field(default_factory=dict)
    # 单调递增的状态版本号，用于拒绝过期上报
    state_version: int = 0
    # 允许的属性名与取值白名单；None 表示不限制
    allowed_properties: dict[str, list[Any]] | None = None
    registered_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class StateReport(BaseModel):
    """设备状态上报请求。state_version 必须严格大于当前版本才接受。"""
    device_id: str
    state: dict[str, Any]
    state_version: int


class CommandStatus(str, enum.Enum):
    PENDING = "pending"
    EXECUTED = "executed"
    REJECTED = "rejected"


class Command(BaseModel):
    command_id: str
    device_id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    status: CommandStatus = CommandStatus.PENDING
    # 幂等键：同一调用方重复提交同一 idempotency_key 返回首次结果
    idempotency_key: str | None = None
    caller: str = ""
    reason: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class CommandRequest(BaseModel):
    device_id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


# ---------------------------------------------------------------------------
# 自动化规则
# ---------------------------------------------------------------------------

class RuleTrigger(BaseModel):
    """当 device_id 的 property 变为/满足 op value 时触发。"""
    device_id: str
    property: str
    op: Literal["eq", "ne", "gt", "lt"] = "eq"
    value: Any


class RuleAction(BaseModel):
    device_id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


class Rule(BaseModel):
    rule_id: str
    name: str = ""
    enabled: bool = True
    trigger: RuleTrigger
    actions: list[RuleAction] = Field(default_factory=list)
    # 规则定义版本：每次更新 +1，执行记录中保存当时版本以便复现
    revision: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class RuleExecution(BaseModel):
    execution_id: str
    rule_id: str
    rule_revision: int
    # 触发事件去重键：同一事件不会触发第二次执行
    event_key: str
    trigger_snapshot: dict[str, Any]
    results: list[dict[str, Any]] = Field(default_factory=list)
    executed_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------

class AuditEntry(BaseModel):
    seq: int
    caller: str
    operation: str
    target: str
    decision: Literal["allow", "deny"]
    detail: str = ""
    at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# 持久化容器
# ---------------------------------------------------------------------------

STORAGE_SCHEMA_VERSION = 2


class StorageData(BaseModel):
    schema_version: int = STORAGE_SCHEMA_VERSION
    devices: dict[str, Device] = Field(default_factory=dict)
    commands: dict[str, Command] = Field(default_factory=dict)
    rules: dict[str, Rule] = Field(default_factory=dict)
    executions: dict[str, RuleExecution] = Field(default_factory=dict)
    audit: list[AuditEntry] = Field(default_factory=list)
    # 已处理事件键集合（重启后仍保证不重复执行）
    processed_events: list[str] = Field(default_factory=list)
    audit_seq: int = 0
